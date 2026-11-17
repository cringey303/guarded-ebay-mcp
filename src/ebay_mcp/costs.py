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

from .gsheets import READ_SCOPE, SheetsClient, SheetsError, a1
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
    if isinstance(v, (int, float, Decimal)):
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
    if isinstance(v, (float, Decimal)):
        if v != int(v):
            return None
        v = int(v)
    s = str(v).strip()
    return s if ITEM_ID_RE.fullmatch(s) else None


def records_from_rows(rows: list[list], item_col="AB", aged_col="AC", floor_col="AD", date_col="A", title_col="B",
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

    CACHE_S = 300

    def __init__(self, sheet_id: str, tab: str, sa_file: str, item_col="AB", aged_col="AC", floor_col="AD",
                 date_col="A", title_col="B", cost_col="I", status_col="J", transport=None, clock=time.time):
        try:
            self.sheets = SheetsClient(sheet_id, sa_file, READ_SCOPE, transport=transport, clock=clock)
        except SheetsError as exc:
            raise CostSourceError(f"GOOGLE_SERVICE_ACCOUNT_FILE: {exc}") from None
        self.tab = tab
        self.cols = dict(item_col=item_col, aged_col=aged_col, floor_col=floor_col, date_col=date_col,
                         title_col=title_col, cost_col=cost_col, status_col=status_col)
        self.last_col = col_letter(max(col_index(c) for c in self.cols.values()))
        self._clock = clock
        self._cache: tuple[dict, float] | None = None

    async def _load(self) -> dict:
        if self._cache and self._cache[1] > self._clock():
            return self._cache[0]
        try:
            rows = await self.sheets.read(a1(self.tab, f"A2:{self.last_col}"))
        except SheetsError as exc:
            raise CostSourceError(str(exc)) from None
        recs = records_from_rows(rows, **self.cols)
        self._cache = (recs, self._clock() + self.CACHE_S)
        return recs

    async def get(self, item_id: str) -> CostRecord | None:
        return (await self._load()).get(item_id)
