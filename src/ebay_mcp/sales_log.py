"""Sale logger: writes eBay sales into the resell sheet.

What one run does:
  1. Read the whole log tab. Row numbers come from that read, never from config,
     so inserting or deleting rows in the sheet is safe.
  2. For each paid eBay order, oldest first: if its order ID is already in the
     order ID column, it was logged before and is skipped.
  3. Otherwise, for every unit sold, pick the oldest unsold row (by purchase
     date) with the same eBay item ID and an allowed status (Listed by default).
     Personal, Nigel and Inventory rows are never picked.
  4. Split the order's money across those rows to the cent and check that the
     net it will write equals what eBay paid out. If anything does not add up,
     the whole order is skipped and reported. An order is never half written.
  5. Dry run (the default) prints every cell it would write. With --write it
     re-reads the sheet, refuses if any target row changed, then writes.

Sheet conventions (your formulas): S = M-N-Q-O+P-R, T = S-I, U = T/I.
Column Z (source) is never overwritten: if Z is empty it gets "CLAUDE";
if Z holds anything else (a Gmail message ID, MANUAL_ENTRY), AA gets "CLAUDE".
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .costs import col_index, col_letter, parse_date, parse_item_id, parse_money
from .gsheets import a1
from .rest import fee_column

CENT = Decimal("0.01")
ZERO = Decimal("0")
CLAUDE = "CLAUDE"
# Your sale columns. The formulas below depend on these letters, so they are fixed.
SALE = {"status": "J", "date": "K", "platform": "L", "price": "M", "fees": "N", "tax": "O",
        "ship_paid": "P", "processing": "Q", "label": "R", "net": "S", "profit": "T", "margin": "U"}
NEVER_PICKED = ("personal", "nigel", "inventory", "expired", "sold")

READY, WRITTEN, ALREADY, WAITING, REVIEW, SKIPPED = (
    "ready", "written", "already logged", "waiting", "needs review", "skipped")


class SalesLogError(Exception):
    pass


class Skip(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class LogConfig:
    tab: str = "Lucas Resell Test"
    date_col: str = "A"
    title_col: str = "B"
    size_col: str = "C"
    color_col: str = "D"
    cost_col: str = "I"
    status_col: str = "J"
    source_col: str = "Z"
    edited_col: str = "AA"
    item_col: str = "AB"
    order_col: str = "AE"
    allowed_statuses: frozenset = frozenset({"listed"})
    tax_in_price: bool = True
    timezone: str = "America/Los_Angeles"
    platform: str = "eBay"

    def __post_init__(self):
        if self.status_col != SALE["status"]:
            raise SalesLogError("The status column must be J for the sale columns to line up.")
        bad = set(self.allowed_statuses) & set(NEVER_PICKED)
        if bad:
            raise SalesLogError(f"These statuses can never be logged as sales: {', '.join(sorted(bad))}")

    @property
    def last_col(self) -> str:
        cols = [self.date_col, self.title_col, self.size_col, self.color_col, self.cost_col, self.source_col,
                self.edited_col, self.item_col, self.order_col, *SALE.values()]
        return col_letter(max(col_index(c) for c in cols))

    @property
    def writable(self) -> set[str]:
        """Every column the logger may ever write. A to I (purchase data) are never in here."""
        return set(SALE.values()) | {self.order_col, self.source_col, self.edited_col}


# ------------------------------------------------------------------ sheet rows
@dataclass
class Row:
    number: int
    cells: list
    cfg: LogConfig

    def get(self, col: str):
        i = col_index(col)
        v = self.cells[i] if i < len(self.cells) else ""
        return "" if v is None else v

    def text(self, col: str) -> str:
        return str(self.get(col)).strip()

    @property
    def item_id(self) -> str | None:
        return parse_item_id(self.get(self.cfg.item_col))

    @property
    def status(self) -> str:
        return self.text(self.cfg.status_col).lower()

    @property
    def purchased(self) -> date | None:
        return parse_date(self.get(self.cfg.date_col))

    @property
    def unsold(self) -> bool:
        return (self.status in self.cfg.allowed_statuses and not self.text(SALE["date"])
                and not self.text(self.cfg.order_col))

    def fingerprint(self) -> tuple:
        """What must not change between planning and writing."""
        c = self.cfg
        return tuple(self.text(col) for col in (c.date_col, c.title_col, c.cost_col, c.status_col, SALE["date"],
                                                c.source_col, c.edited_col, c.item_col, c.order_col))

    def describe(self) -> str:
        c = self.cfg
        extra = ", ".join(x for x in (self.text(c.size_col), self.text(c.color_col)) if x)
        cost = parse_money(self.get(c.cost_col))
        return (f"{self.text(c.title_col)}{f' ({extra})' if extra else ''}, item {self.item_id}"
                f"{f', cost ${cost:.2f}' if cost is not None else ''}")


async def read_rows(sheets, cfg: LogConfig) -> list[Row]:
    values = await sheets.read(a1(cfg.tab, f"A1:{cfg.last_col}"))
    return [Row(i + 1, list(v), cfg) for i, v in enumerate(values)]


# ------------------------------------------------------------------ eBay data
def _amt(obj) -> Decimal:
    return Decimal(str((obj or {}).get("value") or "0")).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    line_item_id: str
    item_id: str
    title: str
    quantity: int
    price: Decimal
    tax: Decimal
    fees: dict[str, Decimal] = field(default_factory=dict)  # sheet column -> amount


@dataclass
class Order:
    order_id: str
    created: str
    lines: list[Line]
    buyer_shipping: Decimal
    label: Decimal = ZERO
    other: dict[str, Decimal] = field(default_factory=dict)  # sheet column -> amount
    payout: Decimal = ZERO

    @property
    def other_total(self) -> Decimal:
        return sum(self.other.values(), ZERO)

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)


def parse_order(raw: dict) -> Order:
    oid = str(raw.get("orderId") or "")
    if raw.get("orderPaymentStatus") != "PAID":
        raise Skip(SKIPPED, f"payment status is {raw.get('orderPaymentStatus')}")
    cancel = (raw.get("cancelStatus") or {}).get("cancelState")
    if cancel not in (None, "", "NONE_REQUESTED"):
        raise Skip(SKIPPED, f"cancel state is {cancel}")
    lines = []
    for li in raw.get("lineItems") or []:
        item_id = parse_item_id(li.get("legacyItemId"))
        qty = li.get("quantity")
        if not item_id or not isinstance(qty, int) or qty < 1:
            raise Skip(REVIEW, "a line item has no valid item ID or quantity")
        tax = sum((_amt(t.get("amount")) for t in li.get("ebayCollectAndRemitTaxes") or []), ZERO)
        lines.append(Line(str(li.get("lineItemId") or ""), item_id, str(li.get("title") or ""), qty,
                          _amt(li.get("lineItemCost")), tax))
    if not lines:
        raise Skip(REVIEW, "order has no line items")
    return Order(oid, str(raw.get("creationDate") or ""), lines, _amt((raw.get("pricingSummary") or {})
                                                                         .get("deliveryCost")))


def add_finances(order: Order, transactions: list[dict]) -> None:
    """Fees, label cost, other order charges and the payout, from Finances getTransactions."""
    types = {t.get("transactionType") for t in transactions}
    if types & {"REFUND", "DISPUTE"}:
        raise Skip(REVIEW, "order has a refund or dispute; log it by hand")
    sales = [t for t in transactions if t.get("transactionType") == "SALE"]
    if not sales:
        raise Skip(WAITING, "eBay has not posted the sale to Finances yet")
    labels = [t for t in transactions if t.get("transactionType") == "SHIPPING_LABEL"]
    if not labels:
        raise Skip(WAITING, "no eBay shipping label yet; it is logged once the label is bought")

    def signed(t):  # debits cost you money, credits (refunds, voided labels) give it back
        return _amt(t.get("amount")) * (1 if t.get("bookingEntry") == "DEBIT" else -1)

    order.label = sum((signed(t) for t in labels), ZERO)
    order.other = {}
    for t in transactions:
        if t.get("transactionType") == "NON_SALE_CHARGE":
            col = SALE[fee_column(t.get("feeType"))]
            order.other[col] = order.other.get(col, ZERO) + signed(t)
    order.payout = sum((-signed(t) for t in sales), ZERO)
    total_fees = sum((_amt(t.get("totalFeeAmount")) for t in sales), ZERO)

    per_line: dict[str, dict[str, Decimal]] = {}
    for t in sales:
        for oli in t.get("orderLineItems") or []:
            buckets = per_line.setdefault(str(oli.get("lineItemId") or ""), {})
            for f in oli.get("marketplaceFees") or []:
                col = SALE[fee_column(f.get("feeType"))]
                buckets[col] = buckets.get(col, ZERO) + _amt(f.get("amount"))

    def total(buckets):
        return sum(buckets.values(), ZERO)

    if (all(line.line_item_id in per_line for line in order.lines)
            and sum(total(per_line[line.line_item_id]) for line in order.lines) == total_fees):
        for line in order.lines:
            line.fees = dict(per_line[line.line_item_id])
        return
    # No per-line breakdown that matches the total: split each kind of fee by item price.
    overall: dict[str, Decimal] = {}
    for buckets in per_line.values():
        for col, amount in buckets.items():
            overall[col] = overall.get(col, ZERO) + amount
    if total(overall) != total_fees:  # fee types unknown: count it all as transaction fees
        overall = {SALE["fees"]: total_fees}
    weights = [line.price for line in order.lines]
    for line in order.lines:
        line.fees = {}
    for col, amount in overall.items():
        for line, share in zip(order.lines, allocate(amount, weights)):
            line.fees[col] = line.fees.get(col, ZERO) + share


def allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split total into len(weights) cent amounts that add up to exactly total."""
    n = len(weights)
    cents = int((total.quantize(CENT, rounding=ROUND_HALF_UP) / CENT))
    sign, cents = (-1 if cents < 0 else 1), abs(cents)
    ws = [max(Decimal(w), ZERO) for w in weights]
    if sum(ws) == 0:
        ws = [Decimal(1)] * n
    raw = [Decimal(cents) * w / sum(ws) for w in ws]
    base = [int(r) for r in raw]
    for i in sorted(range(n), key=lambda i: (-(raw[i] - base[i]), i))[:cents - sum(base)]:
        base[i] += 1
    return [sign * Decimal(b) * CENT for b in base]


# ------------------------------------------------------------------ planning
@dataclass
class Unit:
    row: Row
    line: Line
    values: dict[str, Decimal]

    @property
    def net(self) -> Decimal:
        v = self.values
        return v["M"] - v["N"] - v["Q"] - v["O"] + v["P"] - v["R"]


@dataclass
class Plan:
    order_id: str
    created: str
    status: str
    reason: str = ""
    sale_date: str = ""
    units: list[Unit] = field(default_factory=list)
    cells: list[tuple[str, str]] = field(default_factory=list)
    check: str = ""


def sale_date(created: str, tz: str) -> str:
    """Order time converted to the sheet's time zone, written like your dates (m/d/yyyy)."""
    try:
        d = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).date()
    except ValueError:
        raise Skip(REVIEW, "order has no valid creation date") from None
    return f"{d.month}/{d.day}/{d.year}"


def already_logged(order: Order, rows: list[Row], cfg: LogConfig) -> Plan | None:
    logged = [r for r in rows if r.text(cfg.order_col) == order.order_id]
    if not logged:
        return None
    if len(logged) == order.units:
        return Plan(order.order_id, order.created, ALREADY, f"rows {', '.join(str(r.number) for r in logged)}")
    return Plan(order.order_id, order.created, REVIEW,
                f"{len(logged)} rows carry this order ID but {order.units} units were sold; fix the sheet by hand")


def plan_order(order: Order, rows: list[Row], used: set[int], cfg: LogConfig) -> Plan:
    when = sale_date(order.created, cfg.timezone)
    picks: list[tuple[Line, list[Row]]] = []
    taken = set(used)
    for line in order.lines:
        same_id = [r for r in rows if r.item_id == line.item_id]
        candidates = sorted((r for r in same_id if r.unsold and r.number not in taken),
                            key=lambda r: (r.purchased or date.max, r.number))
        if len(candidates) < line.quantity:
            others = [r for r in same_id if not r.unsold]
            note = (f"; rows {', '.join(str(r.number) for r in others)} have this item ID but are "
                    f"already sold or have a status that is never logged") if others else ""
            raise Skip(REVIEW, f"item {line.item_id} sold {line.quantity}, but only {len(candidates)} unsold row(s) "
                               f"with status {'/'.join(sorted(cfg.allowed_statuses))} carry that item ID{note}")
        chosen = candidates[:line.quantity]
        taken |= {r.number for r in chosen}
        picks.append((line, chosen))

    n = order.units
    even = [Decimal(1)] * n
    ship, label = allocate(order.buyer_shipping, even), allocate(order.label, even)
    other = {col: allocate(amount, even) for col, amount in order.other.items()}
    units, k = [], 0
    for line, chosen in picks:
        q = line.quantity
        prices, taxes = allocate(line.price, [Decimal(1)] * q), allocate(line.tax, [Decimal(1)] * q)
        fees = {col: allocate(amount, [Decimal(1)] * q) for col, amount in line.fees.items()}
        for j, row in enumerate(chosen):
            tax = taxes[j] if cfg.tax_in_price else ZERO
            v = {"M": prices[j] + tax, "N": ZERO, "O": tax, "P": ship[k], "Q": ZERO, "R": label[k]}
            for col, shares in fees.items():
                v[col] += shares[j]
            for col, shares in other.items():
                v[col] += shares[k]
            units.append(Unit(row, line, v))
            k += 1

    written = sum((u.net for u in units), ZERO)
    expected = order.payout - order.label - order.other_total
    check = (f"net written ${written:.2f} vs eBay payout ${order.payout:.2f} - label ${order.label:.2f}"
             f" - other charges ${order.other_total:.2f} = ${expected:.2f}")
    if abs(written - expected) > CENT:
        raise Skip(REVIEW, "money does not reconcile: " + check)
    plan = Plan(order.order_id, order.created, READY, sale_date=when, units=units, check=check)
    plan.cells = [c for u in units for c in unit_cells(u, order.order_id, when, cfg)]
    return plan


def unit_cells(u: Unit, order_id: str, when: str, cfg: LogConfig) -> list[tuple[str, str]]:
    r = u.row.number
    v = u.values
    cells = [
        (SALE["status"], "Sold"), (SALE["date"], when), (SALE["platform"], cfg.platform),
        (SALE["price"], f"{v['M']:.2f}"), (SALE["fees"], f"{v['N']:.2f}"), (SALE["tax"], f"{v['O']:.2f}"),
        (SALE["ship_paid"], f"{v['P']:.2f}"), (SALE["processing"], f"{v['Q']:.2f}"),
        (SALE["label"], f"{v['R']:.2f}"),
        (SALE["net"], f"=M{r}-N{r}-Q{r}-O{r}+P{r}-R{r}"), (SALE["profit"], f"=S{r}-I{r}"),
        (SALE["margin"], f"=T{r}/I{r}"),
        (cfg.order_col, "'" + order_id),  # leading ' keeps it text, so Sheets never reads it as a date
    ]
    source = u.row.text(cfg.source_col)
    if not source:
        cells.append((cfg.source_col, CLAUDE))
    elif source != CLAUDE and u.row.text(cfg.edited_col) != CLAUDE:
        cells.append((cfg.edited_col, CLAUDE))  # Z holds an ID: keep it, mark the edit in AA
    for col, _ in cells:  # defense in depth: nothing outside the allowed columns, Z only when empty
        if col not in cfg.writable or (col == cfg.source_col and source):
            raise SalesLogError(f"Refusing to write column {col} on row {r}")
    return [(a1(cfg.tab, f"{col}{r}"), value) for col, value in cells]


# ------------------------------------------------------------------ running
async def build_plans(rest, rows: list[Row], cfg: LogConfig, days: int, only_order: str | None) -> list[Plan]:
    raws = await rest.get_orders_raw(days=days, limit=200)
    raws = sorted((o for o in raws if not only_order or o.get("orderId") == only_order),
                  key=lambda o: str(o.get("creationDate") or ""))
    used: set[int] = set()
    plans = []
    for raw in raws:
        oid, created = str(raw.get("orderId") or "?"), str(raw.get("creationDate") or "")
        try:
            order = parse_order(raw)
            plan = already_logged(order, rows, cfg)
            if plan is None:
                add_finances(order, await rest.get_order_transactions(oid))
                plan = plan_order(order, rows, used, cfg)
                used |= {u.row.number for u in plan.units}
        except Skip as s:
            plan = Plan(oid, created, s.status, s.reason)
        plans.append(plan)
    return plans


def report(plans: list[Plan], cfg: LogConfig, write: bool) -> str:
    out = [f"Sale log for tab '{cfg.tab}' ({'WRITE' if write else 'dry run, nothing is written'})", ""]
    for p in plans:
        head = f"Order {p.order_id}" + (f", sold {p.sale_date}" if p.sale_date else "") + f": {p.status.upper()}"
        out.append(head + (f" ({p.reason})" if p.reason else ""))
        for u in p.units:
            out.append(f"  Row {u.row.number}: {u.row.describe()}")
            out += [f"    {rng} = {value}" for rng, value in p.cells if rng.endswith(f"!{_col_of(rng)}{u.row.number}")]
        if p.check:
            out.append(f"  Check: {p.check}")
    counts = {s: sum(1 for p in plans if p.status == s) for s in (READY, ALREADY, WAITING, REVIEW, SKIPPED)}
    out += ["", "Summary: " + ", ".join(f"{n} {s}" for s, n in counts.items()) + f" ({len(plans)} orders)"]
    return "\n".join(out)


def _col_of(rng: str) -> str:
    cell = rng.rsplit("!", 1)[1]
    return "".join(ch for ch in cell if ch.isalpha())


async def run(rest, sheets, cfg: LogConfig, *, days: int = 14, only_order: str | None = None, write: bool = False,
              confirm=None, store=None, out=print) -> list[Plan]:
    rows = await read_rows(sheets, cfg)
    plans = await build_plans(rest, rows, cfg, days, only_order)
    out(report(plans, cfg, write))
    ready = [p for p in plans if p.status == READY]
    if not write:
        if ready:
            out("Run again with --write to write these cells.")
        return plans
    if not ready:
        out("Nothing to write.")
        return plans
    cells = [c for p in ready for c in p.cells]
    if confirm is not None and not confirm(len(cells), len(ready)):
        out("Cancelled. Nothing was written.")
        return plans
    fresh = {r.number: r for r in await read_rows(sheets, cfg)}
    for p in ready:
        for u in p.units:
            now = fresh.get(u.row.number)
            if now is None or now.fingerprint() != u.row.fingerprint():
                raise SalesLogError(f"Row {u.row.number} changed since the plan was made (rows inserted, deleted "
                                    "or edited). Nothing was written. Run again.")
    try:
        await sheets.batch_update(cells)
    except Exception as exc:
        _audit(store, ready, cfg, "failed", str(exc))
        raise
    for p in ready:
        p.status = WRITTEN
    _audit(store, ready, cfg, "applied")
    out(f"Wrote {len(cells)} cells for {len(ready)} order(s) to '{cfg.tab}'.")
    return plans


def _audit(store, plans: list[Plan], cfg: LogConfig, status: str, detail: str = "") -> None:
    if store is None:
        return
    for p in plans:
        for u in p.units:
            store.log(change_id=f"sale:{p.order_id}", kind="sale_log", item_id=u.line.item_id, field="sheet_row",
                      old=None, new={"tab": cfg.tab, "row": u.row.number, "order_id": p.order_id},
                      reason="eBay sale logged to the sheet", status=status, detail=detail)


# ------------------------------------------------------------------ command line
def config_from_settings(s) -> LogConfig:
    return LogConfig(tab=s.log_tab, date_col=s.sheet_date_col, title_col=s.sheet_title_col,
                     cost_col=s.sheet_cost_col, status_col=s.sheet_status_col, source_col=s.sheet_source_col,
                     edited_col=s.sheet_edited_col, item_col=s.sheet_item_id_col, order_col=s.sheet_order_col,
                     allowed_statuses=s.log_statuses, tax_in_price=s.log_tax_in_price, timezone=s.log_timezone)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ebay_mcp.sales_log",
                                 description="Log eBay sales into the resell sheet. Dry run unless --write.")
    ap.add_argument("--days", type=int, default=14, help="look at orders from the last N days (max 90)")
    ap.add_argument("--order", help="only this order ID")
    ap.add_argument("--write", action="store_true", help="write to the sheet after showing the plan")
    ap.add_argument("--yes", action="store_true", help="with --write, do not ask before writing")
    args = ap.parse_args(argv)

    from .auth import AuthError, TokenManager
    from .config import ConfigError, Settings
    from .gsheets import READ_SCOPE, WRITE_SCOPE, SheetsClient, SheetsError
    from .http import EbayApiError, EbayHttp, RateLimitExceeded, redact
    from .rest import RestClient
    from .store import Store

    try:
        s = Settings.from_env()
        if not s.log_sheet_id:
            raise ConfigError("Set EBAY_MCP_SHEET_ID (or EBAY_MCP_LOG_SHEET_ID)")
        key = s.google_sa_writer_file if args.write else (s.google_sa_writer_file or s.google_sa_file)
        if not key:
            raise ConfigError("--write needs GOOGLE_SHEETS_WRITER_FILE (a key with edit access)" if args.write
                              else "Set GOOGLE_SHEETS_WRITER_FILE or GOOGLE_SERVICE_ACCOUNT_FILE")
        sheets = SheetsClient(s.log_sheet_id, key, WRITE_SCOPE if args.write else READ_SCOPE)
        store = Store(s.db_path)
        rest = RestClient(s, EbayHttp(s, TokenManager(s), store))
        cfg = config_from_settings(s)

        def ask(n_cells, n_orders):
            return args.yes or input(f"Write {n_cells} cells for {n_orders} order(s) to '{cfg.tab}'? [y/N] ") \
                .strip().lower() == "y"

        asyncio.run(run(rest, sheets, cfg, days=max(1, min(args.days, 90)), only_order=args.order,
                        write=args.write, confirm=ask, store=store))
    except (ConfigError, AuthError, SheetsError, SalesLogError, EbayApiError, RateLimitExceeded, ValueError) as exc:
        sys.exit(f"sale log: {redact(str(exc))}")


if __name__ == "__main__":
    main()
