"""Cost basis lookup. The server reads cost itself; it never accepts a cost
from the model, so a made-up number in chat cannot lower a floor.

Unknown item -> no record -> every write for that item is blocked (fail closed).
If the cost source itself fails, the error propagates and the write is blocked too.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import httpx2 as httpx
import jwt  # PyJWT

from .pricing import CostRecord

# [0-9], not \d: \d also matches non-ASCII digits. fullmatch, not $: $ allows a trailing newline.
ITEM_ID_RE = re.compile(r"[0-9]{9,19}")
TRUTHY = {"y", "yes", "true", "1", "aged", "x"}
SHEETS_EPOCH = date(1899, 12, 30)  # day 0 of Google Sheets serial dates


class CostSourceError(Exception):
    pass


def col_index(letter: str) -> int:
    """A -> 0, Z -> 25, AA -> 26."""
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letter(index: int) -> str:
    s, n = "", index + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_money(v) -> Decimal | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def parse_date(v) -> date | None:
    """Accepts a Sheets serial number (what the API returns for date cells) or common text formats."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(v)) if 1 <= v < 200000 else None
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y %H:%M:%S", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_item_id(v) -> str | None:
    """Sheets returns a 12-digit ID in a number cell as a float; turn it back into digits."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    s = str(v).strip()
    return s if ITEM_ID_RE.fullmatch(s) else None


def records_from_rows(rows: list[list], item_col="V", aged_col="W", floor_col="X", date_col="A", title_col="B",
                      cost_col="I", status_col="J", first_row=2) -> dict[str, CostRecord]:
    """Map sheet rows to CostRecords keyed by eBay item ID.

    If an item ID appears on several rows, keep the highest cost so the floor
    is never computed from the cheaper unit. Rows without a valid item ID, cost
    or date are skipped, which leaves those items with no record (writes blocked).
    """
    ix = {k: col_index(c) for k, c in (("item", item_col), ("aged", aged_col), ("floor", floor_col),
                                       ("date", date_col), ("title", title_col), ("cost", cost_col),
                                       ("status", status_col))}
    width = max(ix.values()) + 1
    out: dict[str, CostRecord] = {}
    for offset, row in enumerate(rows):
        row = list(row) + [""] * (width - len(row))
        item_id = parse_item_id(row[ix["item"]])
        if item_id is None:
            continue
        cost, acquired = parse_money(row[ix["cost"]]), parse_date(row[ix["date"]])
        if cost is None or acquired is None:
            continue
        rec = CostRecord(
            item_id=item_id, cost=cost, acquired=acquired, title=str(row[ix["title"]]),
            aged_ok=str(row[ix["aged"]]).strip().lower() in TRUTHY,
            floor_override=parse_money(row[ix["floor"]]), status=str(row[ix["status"]]),
            sheet_row=first_row + offset,
        )
        prev = out.get(item_id)
        if prev is None or rec.cost > prev.cost:
            out[item_id] = rec
    return out


class JsonCostSource:
    """Local file for the sandbox: {"items": {"<item id>": {"cost": "52.30", "acquired": "2026-03-01", ...}}}"""

    def __init__(self, path: str):
        self.path = Path(path).expanduser()

    async def get(self, item_id: str) -> CostRecord | None:
        if not self.path.exists():
            return None
        try:
            # parse_float=Decimal keeps 52.30 exact instead of going through a float
            data = json.loads(self.path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
            d = data.get("items", {}).get(item_id)
            if not d:
                return None
            cost = parse_money(d["cost"])
            if cost is None:
                raise ValueError("bad cost")
            return CostRecord(
                item_id=item_id, cost=cost, acquired=date.fromisoformat(d["acquired"]),
                title=d.get("title", ""), aged_ok=d.get("aged_ok") is True,
                max_loss_pct=parse_money(d.get("max_loss_pct")),
                floor_override=parse_money(d.get("floor_override")),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CostSourceError(f"Could not read a valid cost record for {item_id} from {self.path.name}") from None


class SheetsCostSource:
    """Reads the cost tab with a read-only Google service account. Results are cached for 5 minutes."""

    SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
    CACHE_S = 300

    def __init__(self, sheet_id: str, tab: str, sa_file: str, item_col="V", aged_col="W", floor_col="X",
                 date_col="A", title_col="B", cost_col="I", status_col="J", transport=None, clock=time.time):
        self.sheet_id, self.tab = sheet_id, tab
        try:
            self.sa = json.loads(Path(sa_file).expanduser().read_text(encoding="utf-8-sig"))
            self.sa["client_email"], self.sa["private_key"], self.sa["token_uri"]
        except (OSError, ValueError, KeyError):
            raise CostSourceError("GOOGLE_SERVICE_ACCOUNT_FILE is missing or not a service account key") from None
        if not str(self.sa["token_uri"]).startswith("https://"):
            raise CostSourceError("Service account token_uri must be https")
        self.cols = dict(item_col=item_col, aged_col=aged_col, floor_col=floor_col, date_col=date_col,
                         title_col=title_col, cost_col=cost_col, status_col=status_col)
        self.last_col = col_letter(max(col_index(c) for c in self.cols.values()))
        self._client = httpx.AsyncClient(timeout=20, transport=transport)
        self._clock = clock
        self._token: tuple[str, float] | None = None
        self._cache: tuple[dict, float] | None = None

    async def _access_token(self) -> str:
        if self._token and self._token[1] - 60 > self._clock():
            return self._token[0]
        now = int(self._clock())
        assertion = jwt.encode(
            {"iss": self.sa["client_email"], "scope": self.SCOPE, "aud": self.sa["token_uri"],
             "iat": now, "exp": now + 3600},
            self.sa["private_key"], algorithm="RS256",
        )
        try:
            r = await self._client.post(self.sa["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            if r.status_code != 200:
                raise CostSourceError(f"Google rejected the service account (HTTP {r.status_code}).")
            body = r.json()
            self._token = (body["access_token"], now + int(body.get("expires_in", 3600)))
        except (httpx.TransportError, ValueError, KeyError):
            raise CostSourceError("Could not get a Google access token.") from None
        return self._token[0]

    async def _load(self) -> dict:
        if self._cache and self._cache[1] > self._clock():
            return self._cache[0]
        rng = quote(f"'{self.tab}'!A2:{self.last_col}", safe="")
        # UNFORMATTED_VALUE + SERIAL_NUMBER: numbers and dates arrive as numbers, whatever the cell format.
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/{rng}"
               "?valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER")
        token = await self._access_token()
        try:
            r = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.TransportError:
            raise CostSourceError("Could not reach Google Sheets.") from None
        if r.status_code != 200:
            hint = " Share the sheet with the service account email as Viewer." if r.status_code == 403 else ""
            raise CostSourceError(f"Google Sheets returned HTTP {r.status_code}.{hint}")
        try:
            # parse_float=Decimal keeps costs exact
            rows = json.loads(r.content, parse_float=Decimal).get("values", [])
        except ValueError:
            raise CostSourceError("Google Sheets returned an unreadable response.") from None
        rows = [[float(c) if isinstance(c, Decimal) and i in self._float_cols else c for i, c in enumerate(row)]
                for row in rows]
        recs = records_from_rows(rows, **self.cols)
        self._cache = (recs, self._clock() + self.CACHE_S)
        return recs

    @property
    def _float_cols(self) -> set:
        # dates and item IDs are not money; hand them to the parsers as plain numbers
        return {col_index(self.cols["date_col"]), col_index(self.cols["item_col"])}

    async def get(self, item_id: str) -> CostRecord | None:
        return (await self._load()).get(item_id)
