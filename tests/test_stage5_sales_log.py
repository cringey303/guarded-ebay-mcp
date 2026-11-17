import json
import tempfile
import unittest
from decimal import Decimal as D

import httpx2 as httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ebay_mcp.config import ConfigError
from ebay_mcp.costs import col_index
from ebay_mcp.gsheets import READ_SCOPE, WRITE_SCOPE, SheetsClient, SheetsError, a1
from ebay_mcp.sales_log import (ALREADY, READY, REVIEW, SKIPPED, WAITING, WRITTEN, LogConfig, SalesLogError,
                                allocate, run)
from ebay_mcp.store import Store

from helpers import make_settings

TAB = "Lucas Resell Test"
CFG = LogConfig(tab=TAB)
A, B, C, DD, E = "123456789012", "223456789012", "323456789012", "423456789012", "523456789012"
MSG = "19d069ed61213cb2"


def row(date, title, cost, status, item="", z="", aa="", ae="", k=""):
    r = [""] * 31
    r[0], r[1], r[2], r[3], r[8], r[9], r[10] = date, title, "S", "Black", D(cost), status, k
    r[col_index("Z")], r[col_index("AA")], r[col_index("AB")], r[col_index("AE")] = z, aa, item, ae
    return r


def grid():
    return [
        ["Purchases"] + [""] * 9 + ["Sales"],
        ["Date", "Item", "Size", "Colorway", "Subtotal", "Shipping", "Tax", "Payment Method", "Total Cost", "Status"],
        row("10/22/2026", "Bones Thermal Crew", "35.00", "Listed", A, MSG),        # 3
        row("10/22/2026", "Bones Thermal Crew", "35.00", "Listed", A, MSG),        # 4
        row(D("45953"), "Bones Thermal Crew", "35.00", "Listed", A, MSG),          # 5: 10/23/2025, oldest
        row("10/22/2026", "Bones Thermal Crew", "35.00", "Personal", A, MSG),      # 6: never picked
        row("9/17/2026", "Dragon Beanie", "43.75", "Listed", B, "MANUAL_ENTRY"),   # 7
        row("9/17/2026", "Vanson Skull Beanie", "52.50", "Listed", C, ""),         # 8: empty source
        row("9/24/2026", "Trooper Hat", "89.85", "Listed", DD, "CLAUDE"),          # 9
        row("2/26/2026", "Boxer Briefs", "47.09", "Inventory", E, MSG),            # 10: never picked
    ]


class FakeSheets:
    def __init__(self, g):
        self.grid, self.writes, self.reads = g, [], 0

    async def read(self, rng):
        self.reads += 1
        return [list(r) for r in self.grid]

    async def batch_update(self, cells):
        self.writes.append(list(cells))
        for rng, value in cells:
            cell = rng.rsplit("!", 1)[1]
            col = "".join(ch for ch in cell if ch.isalpha())
            r = int("".join(ch for ch in cell if ch.isdigit()))
            target = self.grid[r - 1]
            target += [""] * (31 - len(target))
            target[col_index(col)] = value[1:] if value.startswith("'") else value
        return len(cells)

    def cell(self, ref):
        col = "".join(ch for ch in ref if ch.isalpha())
        r = int("".join(ch for ch in ref if ch.isdigit()))
        return self.grid[r - 1][col_index(col)]


def order(oid, created, lines, ship="6.37", paid="PAID", cancel="NONE_REQUESTED"):
    return {"orderId": oid, "creationDate": created, "orderPaymentStatus": paid,
            "cancelStatus": {"cancelState": cancel}, "pricingSummary": {"deliveryCost": {"value": ship}},
            "buyer": {"username": "buyer1", "buyerRegistrationAddress": {"fullName": "Jane Doe"}},
            "lineItems": [{"lineItemId": f"{oid}-L{i}", "legacyItemId": item, "title": "x", "quantity": q,
                           "lineItemCost": {"value": price}, "ebayCollectAndRemitTaxes": [{"amount": {"value": tax}}]}
                          for i, (item, q, price, tax, _fee) in enumerate(lines)]}


def finances(o, lines, label="6.37", other=None, payout_delta="0", extra=()):
    fees = sum(D(f) for *_, f in lines)
    payout = sum(D(p) for _, _, p, _, _ in lines) + D(o["pricingSummary"]["deliveryCost"]["value"]) - fees
    tx = [{"transactionType": "SALE", "bookingEntry": "CREDIT", "amount": {"value": str(payout + D(payout_delta))},
           "totalFeeAmount": {"value": str(fees)},
           "orderLineItems": [{"lineItemId": f"{o['orderId']}-L{i}", "marketplaceFees": [
               {"feeType": "FINAL_VALUE_FEE", "amount": {"value": f}}]} for i, (*_, f) in enumerate(lines)]}]
    if label is not None:
        tx.append({"transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "amount": {"value": label}})
    if other:
        tx.append({"transactionType": "NON_SALE_CHARGE", "bookingEntry": "DEBIT", "amount": {"value": other}})
    return tx + list(extra)


class FakeRest:
    def __init__(self, orders, tx):
        self.orders, self.tx, self.tx_calls = orders, tx, []

    async def get_orders_raw(self, days=30, limit=50):
        return self.orders

    async def get_order_transactions(self, oid):
        self.tx_calls.append(oid)
        return self.tx.get(oid, [])


def setup(specs, g=None, **fin):
    """specs: list of (order_id, created, lines). Returns rest, sheets."""
    orders, tx = [], {}
    for oid, created, lines in specs:
        o = order(oid, created, lines)
        orders.append(o)
        tx[oid] = finances(o, lines, **fin)
    return FakeRest(orders, tx), FakeSheets(g or grid())


ONE = [("11-11111-11111", "2026-11-16T05:00:00.000Z", [(B, 1, "62.00", "4.96", "10.48")])]


async def quiet(rest, sheets, cfg=CFG, **kw):
    lines = []
    plans = await run(rest, sheets, cfg, out=lines.append, **kw)
    return plans, "\n".join(lines)


class SaleLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_prints_exact_cells_and_writes_nothing(self):
        rest, sheets = setup(ONE)
        plans, text = await quiet(rest, sheets)
        self.assertEqual(plans[0].status, READY)
        self.assertEqual(sheets.writes, [])
        self.assertIn("dry run", text)
        for line in ("'Lucas Resell Test'!J7 = Sold", "'Lucas Resell Test'!K7 = 11/15/2026",
                     "'Lucas Resell Test'!M7 = 66.96", "'Lucas Resell Test'!N7 = 10.48",
                     "'Lucas Resell Test'!O7 = 4.96", "'Lucas Resell Test'!P7 = 6.37",
                     "'Lucas Resell Test'!Q7 = 0.00", "'Lucas Resell Test'!R7 = 6.37",
                     "'Lucas Resell Test'!S7 = =M7-N7-Q7-O7+P7-R7", "'Lucas Resell Test'!T7 = =S7-I7",
                     "'Lucas Resell Test'!U7 = =T7/I7", "'Lucas Resell Test'!AE7 = '11-11111-11111",
                     "'Lucas Resell Test'!AA7 = CLAUDE"):
            self.assertIn(line, text)
        self.assertNotIn("Jane", text)  # buyer details never reach the output

    async def test_write_marks_row_sold_and_net_matches_payout(self):
        rest, sheets = setup(ONE)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].status, WRITTEN)
        self.assertEqual((sheets.cell("J7"), sheets.cell("L7"), sheets.cell("AE7")), ("Sold", "eBay", "11-11111-11111"))
        self.assertEqual(sheets.cell("Z7"), "MANUAL_ENTRY")  # source never overwritten
        v = plans[0].units[0].values
        net = v["M"] - v["N"] - v["Q"] - v["O"] + v["P"] - v["R"]
        self.assertEqual(net, D("51.52"))  # = payout 57.89 - label 6.37
        self.assertEqual(len(sheets.writes), 1)  # one atomic batch

    async def test_never_writes_purchase_columns(self):
        rest, sheets = setup(ONE)
        plans, _ = await quiet(rest, sheets)
        cols = {rng.rsplit("!", 1)[1].rstrip("0123456789") for rng, _ in plans[0].cells}
        self.assertTrue(cols <= CFG.writable)
        self.assertFalse(cols & set("ABCDEFGHI"))

    async def test_running_twice_does_not_double_log(self):
        rest, sheets = setup(ONE)
        await quiet(rest, sheets, write=True)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].status, ALREADY)
        self.assertEqual(len(sheets.writes), 1)
        self.assertEqual(rest.tx_calls, ["11-11111-11111"])  # no Finances call for a logged order

    async def test_multi_quantity_marks_oldest_rows_one_per_unit(self):
        specs = [("22-22222-22222", "2026-11-16T20:00:00.000Z", [(A, 2, "70.01", "5.61", "11.01")])]
        rest, sheets = setup(specs)
        plans, _ = await quiet(rest, sheets, write=True)
        units = plans[0].units
        self.assertEqual([u.row.number for u in units], [5, 3])  # oldest purchase date, then top row
        self.assertEqual([u.values["M"] for u in units], [D("35.01") + D("2.81"), D("35.00") + D("2.80")])
        self.assertEqual(sum(u.values["R"] for u in units), D("6.37"))
        self.assertEqual((sheets.cell("J4"), sheets.cell("J6")), ("Listed", "Personal"))

    async def test_two_orders_for_one_listing_use_different_rows(self):
        specs = [("33-1", "2026-11-14T20:00:00.000Z", [(A, 1, "35.00", "2.80", "5.10")]),
                 ("33-2", "2026-11-15T20:00:00.000Z", [(A, 1, "35.00", "2.80", "5.10")])]
        rest, sheets = setup(specs)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual([p.units[0].row.number for p in plans], [5, 3])

    async def test_order_with_two_listings_splits_shipping_and_label(self):
        specs = [("44-4", "2026-11-16T20:00:00.000Z", [(B, 1, "40.00", "3.20", "6.00"), (C, 1, "50.00", "4.00", "7.50")])]
        rest, sheets = setup(specs, label="7.01")
        rest.orders[0]["pricingSummary"]["deliveryCost"]["value"] = "10.00"
        rest.tx["44-4"] = finances(rest.orders[0], specs[0][2], label="7.01")
        plans, _ = await quiet(rest, sheets)
        self.assertEqual(plans[0].status, READY, plans[0].reason)
        u1, u2 = plans[0].units
        self.assertEqual((u1.values["P"], u2.values["P"]), (D("5.00"), D("5.00")))
        self.assertEqual((u1.values["R"], u2.values["R"]), (D("3.51"), D("3.50")))
        self.assertEqual((u1.values["N"], u2.values["N"]), (D("6.00"), D("7.50")))  # per-line fees from eBay

    async def test_never_picks_personal_nigel_or_inventory_rows(self):
        specs = [("55-5", "2026-11-16T20:00:00.000Z", [(E, 1, "50.00", "4.00", "8.00")])]
        rest, sheets = setup(specs)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].status, REVIEW)
        self.assertIn("never logged", plans[0].reason)
        self.assertEqual(sheets.writes, [])
        with self.assertRaises(SalesLogError):
            LogConfig(allowed_statuses=frozenset({"listed", "personal"}))

    async def test_not_enough_rows_skips_the_whole_order(self):
        specs = [("66-6", "2026-11-16T20:00:00.000Z", [(B, 1, "40.00", "3.20", "6.00"), (A, 4, "140.00", "11.20", "22.00")])]
        rest, sheets = setup(specs)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].status, REVIEW)
        self.assertIn("only 3 unsold", plans[0].reason)
        self.assertIn("6", plans[0].reason)  # the Personal row is named
        self.assertEqual(sheets.writes, [])  # item B was not half logged either

    async def test_source_column_rules(self):
        specs = [("77-1", "2026-11-16T20:00:00.000Z", [(C, 1, "60.00", "4.80", "9.00")]),
                 ("77-2", "2026-11-16T21:00:00.000Z", [(DD, 1, "95.00", "7.60", "14.00")]),
                 ("77-3", "2026-11-16T22:00:00.000Z", [(A, 1, "35.00", "2.80", "5.10")])]
        rest, sheets = setup(specs)
        await quiet(rest, sheets, write=True)
        self.assertEqual((sheets.cell("Z8"), sheets.cell("AA8")), ("CLAUDE", ""))        # empty Z -> Z
        self.assertEqual((sheets.cell("Z9"), sheets.cell("AA9")), ("CLAUDE", ""))        # already CLAUDE
        self.assertEqual((sheets.cell("Z5"), sheets.cell("AA5")), (MSG, "CLAUDE"))       # message ID kept

    async def test_waits_for_label_and_skips_refunds_cancels_and_unpaid(self):
        rest, sheets = setup(ONE, label=None)
        self.assertEqual((await quiet(rest, sheets))[0][0].status, WAITING)
        refund = {"transactionType": "REFUND", "bookingEntry": "DEBIT", "amount": {"value": "10.00"}}
        rest, sheets = setup(ONE, extra=[refund])
        self.assertEqual((await quiet(rest, sheets))[0][0].status, REVIEW)
        rest, sheets = setup(ONE)
        rest.tx = {}
        self.assertEqual((await quiet(rest, sheets))[0][0].status, WAITING)
        for key, value in (("orderPaymentStatus", "PENDING"), ("cancelStatus", {"cancelState": "CANCELED"})):
            rest, sheets = setup(ONE)
            rest.orders[0][key] = value
            plans, _ = await quiet(rest, sheets, write=True)
            self.assertEqual(plans[0].status, SKIPPED)
            self.assertEqual(sheets.writes, [])

    async def test_money_that_does_not_reconcile_is_not_written(self):
        rest, sheets = setup(ONE, payout_delta="1.00")
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].status, REVIEW)
        self.assertIn("does not reconcile", plans[0].reason)
        self.assertEqual(sheets.writes, [])

    async def test_promoted_listing_fee_goes_into_fees_so_net_matches(self):
        rest, sheets = setup(ONE, other="3.10")
        rest.tx[ONE[0][0]] = finances(rest.orders[0], ONE[0][2], other="3.10")
        plans, _ = await quiet(rest, sheets)
        self.assertEqual(plans[0].status, READY, plans[0].reason)
        self.assertEqual(plans[0].units[0].values["N"], D("13.58"))

    async def test_fees_go_to_the_closest_column_by_fee_type(self):
        rest, sheets = setup(ONE)
        tx = rest.tx[ONE[0][0]]
        tx[0]["orderLineItems"][0]["marketplaceFees"] = [
            {"feeType": "FINAL_VALUE_FEE", "amount": {"value": "8.00"}},
            {"feeType": "PAYMENT_PROCESSING_FEE", "amount": {"value": "2.00"}},
            {"feeType": "FEE_TAX", "amount": {"value": "0.48"}}]
        tx.append({"transactionType": "NON_SALE_CHARGE", "feeType": "AD_FEE", "bookingEntry": "DEBIT",
                   "amount": {"value": "1.00"}})
        plans, _ = await quiet(rest, sheets)
        self.assertEqual(plans[0].status, READY, plans[0].reason)
        v = plans[0].units[0].values
        self.assertEqual((v["N"], v["Q"], v["O"]), (D("9.00"), D("2.00"), D("4.96") + D("0.48")))
        self.assertEqual(sheets.writes, [])

    async def test_fee_types_kept_when_split_by_price(self):
        specs = [("88-8", "2026-11-16T20:00:00.000Z", [(B, 1, "40.00", "0", "4.00"), (C, 1, "60.00", "0", "6.00")])]
        rest, sheets = setup(specs)
        sale = rest.tx["88-8"][0]
        sale["orderLineItems"] = [{"lineItemId": "unknown", "marketplaceFees": [
            {"feeType": "FINAL_VALUE_FEE", "amount": {"value": "8.00"}},
            {"feeType": "PAYMENT_PROCESSING_FEE", "amount": {"value": "2.00"}}]}]
        plans, _ = await quiet(rest, sheets)
        self.assertEqual(plans[0].status, READY, plans[0].reason)
        u1, u2 = plans[0].units
        self.assertEqual((u1.values["N"], u1.values["Q"], u2.values["N"], u2.values["Q"]),
                         (D("3.20"), D("0.80"), D("4.80"), D("1.20")))

    async def test_tax_can_be_left_out_of_price(self):
        rest, sheets = setup(ONE)
        plans, _ = await quiet(rest, sheets, LogConfig(tab=TAB, tax_in_price=False))
        v = plans[0].units[0].values
        self.assertEqual((v["M"], v["O"]), (D("62.00"), D("0")))
        self.assertEqual(plans[0].status, READY)

    async def test_row_numbers_follow_the_sheet_not_config(self):
        g = grid()
        g.insert(2, row("1/1/2026", "New row someone added", "10.00", "Listed"))
        rest, sheets = setup(ONE, g=g)
        plans, _ = await quiet(rest, sheets, write=True)
        self.assertEqual(plans[0].units[0].row.number, 8)
        self.assertEqual(sheets.cell("J8"), "Sold")

    async def test_refuses_to_write_if_the_sheet_changed_after_planning(self):
        rest, sheets = setup(ONE)

        def someone_inserts_a_row(*_):
            sheets.grid.insert(2, row("1/1/2026", "Inserted meanwhile", "10.00", "Listed"))
            return True

        with self.assertRaises(SalesLogError):
            await run(rest, sheets, CFG, write=True, confirm=someone_inserts_a_row, out=lambda *_: None)
        self.assertEqual(sheets.writes, [])

    async def test_cancelled_confirmation_writes_nothing(self):
        rest, sheets = setup(ONE)
        await run(rest, sheets, CFG, write=True, confirm=lambda *_: False, out=lambda *_: None)
        self.assertEqual(sheets.writes, [])

    async def test_writes_are_recorded_in_the_audit_log(self):
        rest, sheets = setup(ONE)
        store = Store()
        await run(rest, sheets, CFG, write=True, store=store, out=lambda *_: None)
        log = store.list_audit(B)[0]
        self.assertEqual((log["kind"], log["status"], log["new_value"]["row"]), ("sale_log", "applied", 7))

    def test_allocate_adds_up_to_the_cent(self):
        self.assertEqual(allocate(D("10.00"), [D(1)] * 3), [D("3.34"), D("3.33"), D("3.33")])
        self.assertEqual(allocate(D("0.01"), [D(1)] * 2), [D("0.01"), D("0.00")])
        self.assertEqual(allocate(D("-1.00"), [D(1)] * 3), [D("-0.34"), D("-0.33"), D("-0.33")])
        self.assertEqual(allocate(D("9.00"), [D("1"), D("2")]), [D("3.00"), D("6.00")])
        self.assertEqual(allocate(D("5.00"), [D(0), D(0)]), [D("2.50"), D("2.50")])


class SettingsTests(unittest.TestCase):
    def test_defaults_point_at_the_test_tab(self):
        s = make_settings(EBAY_MCP_SHEET_ID="1oiY3MeNXI4um-test-sheet-id")
        self.assertEqual((s.log_tab, s.log_sheet_id), ("Lucas Resell Test", "1oiY3MeNXI4um-test-sheet-id"))
        self.assertEqual((s.sheet_item_id_col, s.sheet_aged_col, s.sheet_floor_col, s.sheet_order_col),
                         ("AB", "AC", "AD", "AE"))
        self.assertEqual(s.log_statuses, frozenset({"listed"}))

    def test_sold_can_never_be_a_log_status(self):
        with self.assertRaises(ConfigError):
            make_settings(EBAY_MCP_LOG_STATUSES="Listed,Sold")


class SheetsClientTests(unittest.IsolatedAsyncioTestCase):
    def key_file(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"client_email": "w@p.iam.gserviceaccount.com", "private_key": pem,
                   "token_uri": "https://oauth2.googleapis.com/token"}, f)
        f.close()
        return f.name

    async def test_batch_update_payload(self):
        seen = []

        def handler(req):
            seen.append(req)
            if req.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "ya29.w", "expires_in": 3600})
            return httpx.Response(200, json={"totalUpdatedCells": 2})

        c = SheetsClient("sheet-id-1234567890", self.key_file(), WRITE_SCOPE, transport=httpx.MockTransport(handler))
        n = await c.batch_update([(a1("Lucas Resell Test", "J7"), "Sold"), (a1("It's", "S7"), "=M7-N7")])
        self.assertEqual(n, 2)
        body = json.loads(seen[-1].content)
        self.assertEqual(body["valueInputOption"], "USER_ENTERED")
        self.assertEqual([d["range"] for d in body["data"]], ["'Lucas Resell Test'!J7", "'It''s'!S7"])
        self.assertTrue(str(seen[-1].url).endswith("/values:batchUpdate"))

    async def test_read_only_client_cannot_write(self):
        c = SheetsClient("sheet-id-1234567890", self.key_file(), READ_SCOPE)
        with self.assertRaises(SheetsError):
            await c.batch_update([(a1(TAB, "J7"), "Sold")])


if __name__ == "__main__":
    unittest.main()
