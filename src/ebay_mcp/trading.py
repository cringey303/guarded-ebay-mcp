"""Trading API (XML) client for listings created on the eBay website/app.

Listings made on the website or app are not Inventory API listings, so they
can only be revised through the Trading API.

Requests are built from validated values only (digits-only item IDs, integers,
Decimals and an allowlisted package type). Responses are parsed with defusedxml
so a malicious or broken response cannot trigger XXE or entity expansion.
"""
from __future__ import annotations

from decimal import Decimal

from defusedxml import ElementTree as SafeET

from .config import Settings
from .costs import ITEM_ID_RE
from .http import EbayApiError, EbayHttp

NS = "urn:ebay:apis:eBLBaseComponents"
_N = {"e": NS}


# ShippingPackageCodeType values that make sense for US apparel and shoes.
PACKAGE_TYPES = frozenset({
    "Letter", "LargeEnvelope", "PackageThickEnvelope", "ParcelOrPaddedEnvelope", "PaddedBags",
    "MailingBoxes", "ExtraLargePack", "USPSLargePack", "USPSFlatRateEnvelope", "VeryLargePack", "None",
})
# Trading API reports an expired or invalid OAuth token as an XML error, not HTTP 401.
AUTH_ERROR_CODES = frozenset({"931", "932", "16110", "21916984", "21917053"})


class TradingError(EbayApiError):
    pass


def validate_item_id(item_id: str) -> str:
    item_id = str(item_id).strip()
    if not ITEM_ID_RE.fullmatch(item_id):
        raise ValueError("Invalid eBay item ID (must be 9-19 digits).")
    return item_id


def validate_package_type(package_type: str | None) -> str | None:
    if package_type in (None, ""):
        return None
    if package_type not in PACKAGE_TYPES:
        raise ValueError("Unknown package_type. Use one of: " + ", ".join(sorted(PACKAGE_TYPES)))
    return package_type


def _whole(v, name: str) -> int:
    """Whole, non-negative number for XML. Rejects bools, fractions and strings with markup."""
    try:
        d = Decimal(str(v))
        ok = not isinstance(v, bool) and d.is_finite() and d == d.to_integral_value() and 0 <= d < 1000
    except ArithmeticError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a whole number")
    return int(d)


def build_request(call: str, inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<{call}Request xmlns="{NS}">'
            f"<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>{inner}</{call}Request>").encode()


def parse_response(body: bytes):
    root = SafeET.fromstring(body)
    ack = root.findtext("e:Ack", default="", namespaces=_N)
    errors, warnings = [], []
    for err in root.findall("e:Errors", _N):
        entry = {
            "code": err.findtext("e:ErrorCode", default="", namespaces=_N),
            "message": err.findtext("e:LongMessage", default="", namespaces=_N)
            or err.findtext("e:ShortMessage", default="", namespaces=_N),
        }
        (errors if err.findtext("e:SeverityCode", namespaces=_N) == "Error" else warnings).append(entry)
    if ack in ("Failure", "PartialFailure") and errors:
        msg = "; ".join(f"{e['code']}: {e['message']}" for e in errors)
        if any(e["code"] == "518" for e in errors):
            msg = "Trading API daily call limit reached (518). " + msg
        raise TradingError(200, msg, errors)
    return root, warnings


def _price(p) -> str:
    if not isinstance(p, Decimal) or not p.is_finite() or p <= 0 or p != p.quantize(Decimal("0.01")):
        raise ValueError("Price must be a positive Decimal in whole cents")
    return f"{p:.2f}"


def _dim(pkg, path):
    """Dimensions come back like '10' or '10.0'; whole inches or None (0 means not set)."""
    v = _t(pkg, path)
    try:
        n = int(Decimal(v)) if v else 0
    except ArithmeticError:
        n = 0
    return n or None


def _t(el, path, default=None):
    v = el.findtext(path, default=default, namespaces=_N)
    return v.strip() if isinstance(v, str) else v


def _item_summary(it) -> dict:
    price = _t(it, "e:SellingStatus/e:CurrentPrice") or _t(it, "e:BuyItNowPrice") or _t(it, "e:StartPrice")
    return {
        "item_id": _t(it, "e:ItemID"),
        "title": _t(it, "e:Title", ""),
        "price": price,
        "watchers": int(_t(it, "e:WatchCount", "0") or 0),
        "start_time": _t(it, "e:ListingDetails/e:StartTime"),
        "quantity_available": _t(it, "e:QuantityAvailable"),
        "listing_type": _t(it, "e:ListingType"),
    }


class TradingClient:
    def __init__(self, settings: Settings, http: EbayHttp):
        self.s, self.http = settings, http

    async def call(self, call: str, inner: str, idempotent: bool = True):
        for attempt in (1, 2):
            resp = await self.http.request(
                "trading", "POST", self.s.urls["trading"], header_style="iaf", idempotent=idempotent,
                headers={
                    "X-EBAY-API-CALL-NAME": call,
                    "X-EBAY-API-SITEID": str(self.s.site_id),
                    "X-EBAY-API-COMPATIBILITY-LEVEL": str(self.s.trading_compat_level),
                    "Content-Type": "text/xml",
                },
                content=build_request(call, inner),
            )
            if resp.status_code >= 400:
                raise TradingError(resp.status_code, f"HTTP {resp.status_code} from Trading API")
            try:
                return parse_response(resp.content)
            except TradingError as exc:
                # An expired token was rejected before eBay did anything, so one retry is safe.
                if attempt == 1 and any(e["code"] in AUTH_ERROR_CODES for e in exc.errors):
                    self.http.tokens.invalidate("user")
                    continue
                raise

    async def get_active_listings(self, max_pages: int = 5) -> list[dict]:
        out: list[dict] = []
        page, total_pages = 1, 1
        while page <= min(total_pages, max_pages):
            root, _ = await self.call("GetMyeBaySelling", (
                "<ActiveList><Include>true</Include>"
                f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
                "</ActiveList><DetailLevel>ReturnAll</DetailLevel>"))
            al = root.find("e:ActiveList", _N)
            if al is None:
                break
            total_pages = int(_t(al, "e:PaginationResult/e:TotalNumberOfPages", "1") or 1)
            out += [_item_summary(it) for it in al.findall("e:ItemArray/e:Item", _N)]
            page += 1
        return out

    async def get_item(self, item_id: str) -> dict:
        item_id = validate_item_id(item_id)
        root, _ = await self.call("GetItem", f"<ItemID>{item_id}</ItemID><IncludeWatchCount>true</IncludeWatchCount>")
        it = root.find("e:Item", _N)
        if it is None:
            raise TradingError(404, f"Item {item_id} not found")
        d = _item_summary(it)
        d["start_price"] = _t(it, "e:StartPrice")
        d["category"] = _t(it, "e:PrimaryCategory/e:CategoryName", "")
        d["category_id"] = _t(it, "e:PrimaryCategory/e:CategoryID", "")
        d["has_variations"] = it.find("e:Variations", _N) is not None
        d["status"] = _t(it, "e:SellingStatus/e:ListingStatus")
        pkg = it.find("e:ShippingPackageDetails", _N)
        if pkg is not None:
            d["package"] = {
                "weight_lb": int(Decimal(_t(pkg, "e:WeightMajor", "0") or 0)),
                "weight_oz": int(Decimal(_t(pkg, "e:WeightMinor", "0") or 0)),
                "length_in": _dim(pkg, "e:PackageLength"),
                "width_in": _dim(pkg, "e:PackageWidth"),
                "depth_in": _dim(pkg, "e:PackageDepth"),
                "package_type": _t(pkg, "e:ShippingPackage"),
            }
        else:
            d["package"] = None
        return d

    async def revise_prices(self, updates: list[tuple[str, Decimal]]) -> list[dict]:
        """ReviseInventoryStatus, max 4 listings per call (eBay limit)."""
        if not 1 <= len(updates) <= 4:
            raise ValueError("ReviseInventoryStatus takes 1-4 listings per call")
        inner = "".join(
            f"<InventoryStatus><ItemID>{validate_item_id(i)}</ItemID><StartPrice>{_price(p)}</StartPrice></InventoryStatus>"
            for i, p in updates)
        root, warnings = await self.call("ReviseInventoryStatus", inner, idempotent=False)
        res = [{"item_id": _t(s, "e:ItemID"), "price": _t(s, "e:StartPrice")}
               for s in root.findall("e:InventoryStatus", _N)]
        return res + ([{"warnings": warnings}] if warnings else [])

    async def revise_package(self, item_id: str, weight_lb: int, weight_oz: int,
                             length_in=None, width_in=None, depth_in=None, package_type=None) -> dict:
        item_id = validate_item_id(item_id)
        package_type = validate_package_type(package_type)
        parts = ["<MeasurementUnit>English</MeasurementUnit>"]
        if depth_in is not None:
            parts.append(f'<PackageDepth unit="inches">{_whole(depth_in, "depth_in")}</PackageDepth>')
        if length_in is not None:
            parts.append(f'<PackageLength unit="inches">{_whole(length_in, "length_in")}</PackageLength>')
        if width_in is not None:
            parts.append(f'<PackageWidth unit="inches">{_whole(width_in, "width_in")}</PackageWidth>')
        if package_type:
            parts.append(f"<ShippingPackage>{package_type}</ShippingPackage>")
        parts.append(f'<WeightMajor unit="lbs">{_whole(weight_lb, "weight_lb")}</WeightMajor>')
        parts.append(f'<WeightMinor unit="oz">{_whole(weight_oz, "weight_oz")}</WeightMinor>')
        inner = f"<Item><ItemID>{item_id}</ItemID><ShippingPackageDetails>{''.join(parts)}</ShippingPackageDetails></Item>"
        root, warnings = await self.call("ReviseFixedPriceItem", inner, idempotent=False)
        return {"item_id": _t(root, "e:ItemID"), "warnings": warnings}
