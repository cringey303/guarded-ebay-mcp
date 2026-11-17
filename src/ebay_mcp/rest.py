"""REST wrappers: Fulfillment (orders), Finances (fees, labels), Negotiation (offers), Browse (comps)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import Settings
from .costs import ITEM_ID_RE
from .http import EbayHttp, raise_for_rest
from .pricing import money

# Order IDs look like 12-34567-89012 (older ones: itemId-transactionId). Digits and dashes only.
ORDER_ID_RE = re.compile(r"[0-9]{1,20}(-[0-9]{1,20}){0,2}")


def validate_order_id(order_id: str) -> str:
    order_id = str(order_id).strip()
    if not 5 <= len(order_id) <= 40 or not ORDER_ID_RE.fullmatch(order_id):
        raise ValueError("Invalid order ID (expected digits and dashes, like 12-34567-89012).")
    return order_id


def _amt(obj) -> Decimal:
    return Decimal(str((obj or {}).get("value", "0") or "0"))


def summarize_order(o: dict) -> dict:
    """Keep what the sheet needs. Buyer address and name are dropped on purpose."""
    ps = o.get("pricingSummary", {})
    items = []
    ship_by = None
    tax = Decimal(0)
    for li in o.get("lineItems", []):
        sb = (li.get("lineItemFulfillmentInstructions") or {}).get("shipByDate")
        ship_by = min(filter(None, [ship_by, sb])) if sb else ship_by
        for t in li.get("ebayCollectAndRemitTaxes", []) or []:
            tax += _amt(t.get("amount"))
        items.append({"item_id": li.get("legacyItemId"), "title": li.get("title"),
                      "quantity": li.get("quantity"), "price": str(_amt(li.get("lineItemCost")))})
    return {
        "order_id": o.get("orderId"),
        "created": o.get("creationDate"),
        "status": o.get("orderFulfillmentStatus"),
        "payment_status": o.get("orderPaymentStatus"),
        "items": items,
        "item_subtotal": str(_amt(ps.get("priceSubtotal"))),
        "buyer_shipping_paid": str(_amt(ps.get("deliveryCost"))),
        "ebay_collected_tax": str(money(tax)),
        "order_total": str(_amt(ps.get("total"))),
        "ship_by": ship_by,
    }


def fee_column(fee_type) -> str:
    """Which sheet column an eBay fee belongs in: "tax" only for fees that say tax, "processing"
    for payment or processing fees, "fees" (Transaction Fees) for everything else."""
    name = str(fee_type or "").upper()
    if "TAX" in name:
        return "tax"
    if "PAYMENT" in name or "PROCESSING" in name:
        return "processing"
    return "fees"


def summarize_fees(transactions: list[dict]) -> dict:
    """Turn Finances getTransactions output for one order into fee/label totals."""
    fees = Decimal(0)
    by_type: dict[str, Decimal] = {}
    payout = Decimal(0)
    label = Decimal(0)
    other = Decimal(0)
    types = sorted({str(t.get("transactionType")) for t in transactions})
    for t in transactions:
        typ = t.get("transactionType")
        sign = 1 if t.get("bookingEntry") == "DEBIT" else -1
        if typ == "SALE":
            fees += _amt(t.get("totalFeeAmount"))
            payout += _amt(t.get("amount"))
            for li in t.get("orderLineItems", []) or []:
                for f in li.get("marketplaceFees", []) or []:
                    k = f.get("feeType", "OTHER")
                    by_type[k] = by_type.get(k, Decimal(0)) + _amt(f.get("amount"))
        elif typ == "SHIPPING_LABEL":
            label += sign * _amt(t.get("amount"))  # voided label refunds come back as CREDIT
        elif typ == "NON_SALE_CHARGE":
            other += sign * _amt(t.get("amount"))  # e.g. promoted listing fee tied to the order
    # Sort the fees into sheet columns by fee type. If the breakdown does not add up to the
    # total, count the whole total as transaction fees.
    columns = {"fees": Decimal(0), "processing": Decimal(0), "tax": Decimal(0)}
    if sum(by_type.values(), Decimal(0)) == fees:
        for k, v in by_type.items():
            columns[fee_column(k)] += v
    else:
        columns["fees"] = fees
    return {
        "transaction_fees": str(money(fees)),
        "fee_columns": {k: str(money(v)) for k, v in columns.items()},
        "fee_breakdown": {k: str(money(v)) for k, v in by_type.items()},
        "label_cost": str(money(label)) if any(t.get("transactionType") == "SHIPPING_LABEL" for t in transactions) else None,
        "other_order_charges": str(money(other)),
        "sale_payout": str(money(payout)),
        "transaction_types": types,
    }


class RestClient:
    def __init__(self, settings: Settings, http: EbayHttp):
        self.s, self.http = settings, http
        self.mkt = {"X-EBAY-C-MARKETPLACE-ID": settings.marketplace_id}

    async def get_orders(self, days: int = 30, limit: int = 50) -> list[dict]:
        return [summarize_order(o) for o in await self.get_orders_raw(days, limit)]

    async def get_orders_raw(self, days: int = 30, limit: int = 50) -> list[dict]:
        """Full order objects. They include buyer details, so they stay inside the server
        (the sale logger uses them) and are never returned to Claude."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        r = await self.http.request("fulfillment", "GET", f"{self.s.urls['api']}/sell/fulfillment/v1/order",
                                    params={"filter": f"creationdate:[{since}..]", "limit": str(limit)})
        if r.status_code == 204:
            return []
        raise_for_rest(r)
        return r.json().get("orders", [])

    async def get_order(self, order_id: str) -> dict:
        oid = validate_order_id(order_id)
        r = await self.http.request("fulfillment", "GET", f"{self.s.urls['api']}/sell/fulfillment/v1/order/{oid}")
        raise_for_rest(r)
        return summarize_order(r.json())

    async def get_order_transactions(self, order_id: str) -> list[dict]:
        oid = validate_order_id(order_id)
        out: list[dict] = []
        for page in range(5):
            r = await self.http.request("finances", "GET", f"{self.s.urls['apiz']}/sell/finances/v1/transaction",
                                        headers=self.mkt,
                                        params={"filter": f"orderId:{{{oid}}}", "limit": "200",
                                                "offset": str(page * 200)})
            if r.status_code == 204:
                break
            raise_for_rest(r)
            body = r.json()
            out += body.get("transactions", [])
            if not body.get("next"):
                break
        return out

    async def find_eligible_items(self) -> set[str]:
        out: set[str] = set()
        for page in range(10):
            r = await self.http.request("negotiation", "GET",
                                        f"{self.s.urls['api']}/sell/negotiation/v1/find_eligible_items",
                                        headers=self.mkt, params={"limit": "200", "offset": str(page * 200)})
            if r.status_code == 204:
                break
            raise_for_rest(r)
            body = r.json()
            out |= {str(e.get("listingId")) for e in body.get("eligibleItems", [])}
            if not body.get("next"):
                break
        return out

    async def send_offer(self, listing_id: str, price: Decimal, message: str = "", quantity: int = 1) -> dict:
        if not ITEM_ID_RE.fullmatch(str(listing_id)):
            raise ValueError("Invalid eBay item ID (must be 9-19 digits).")
        body = {
            "offeredItems": [{"listingId": listing_id, "quantity": quantity,
                              "price": {"currency": "USD", "value": f"{money(price)}"}}],
            "allowCounterOffer": False,
            "offerDuration": {"unit": "DAY", "value": 2},
        }
        if message:
            body["message"] = message[:2000]
        r = await self.http.request("negotiation", "POST",
                                    f"{self.s.urls['api']}/sell/negotiation/v1/send_offer_to_interested_buyers",
                                    headers={**self.mkt, "Content-Type": "application/json"}, json=body,
                                    idempotent=False)  # never send the same offer twice
        raise_for_rest(r)
        offers = r.json().get("offers", [])
        return {"offers_sent": len(offers), "offer_ids": [o.get("offerId") for o in offers]}

    async def browse_search(self, query: str, limit: int = 20, condition_new: bool = True) -> list[dict]:
        flt = "buyingOptions:{FIXED_PRICE}"
        if condition_new:
            flt += ",conditions:{NEW}"
        r = await self.http.request("browse", "GET", f"{self.s.urls['api']}/buy/browse/v1/item_summary/search",
                                    auth="app", headers=self.mkt,
                                    params={"q": query[:100], "limit": str(min(limit, 50)), "filter": flt})
        raise_for_rest(r)
        out = []
        for it in r.json().get("itemSummaries", []):
            ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost")
            out.append({"title": it.get("title"), "price": str(_amt(it.get("price"))),
                        "shipping": None if ship is None else str(_amt(ship)),
                        "condition": it.get("condition"), "url": it.get("itemWebUrl")})
        return out
