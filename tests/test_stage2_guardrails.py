import inspect
import json
import tempfile
import time
import unittest
from datetime import date
from decimal import Decimal as D

import httpx2 as httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ebay_mcp import pricing
from ebay_mcp.costs import (CostSourceError, JsonCostSource, SheetsCostSource, parse_date, parse_item_id,
                            records_from_rows)
from ebay_mcp.pricing import (ATHLETIC_SHOES, STANDARD, CostRecord, FeeModel, PricingPolicy, break_even,
                              category_for, ceil_cent, check_offer, check_price, check_weight, compute_floor, fees,
                              months_held, net, offer_price_from_discount, quick_break_even, weight_limit_lb)
from ebay_mcp.store import Store

P = PricingPolicy()
OCT = date(2026, 10, 22)
FEB = date(2027, 2, 15)
JUN = date(2026, 6, 15)


def rec(cost="50.00", acquired=date(2026, 1, 1), **kw):
    return CostRecord(item_id="123456789012", cost=D(cost), acquired=acquired, **kw)


def brute_force_break_even(cost, ship, label, category):
    p = D("0.01")
    while net(p, ship, label, category) < cost:
        p += D("0.01")
    return p


class FeeModelTests(unittest.TestCase):
    def test_standard_fee(self):
        # 13.6% of (58.99 + 0) x 1.07, plus $0.40
        self.assertEqual(fees(D("58.99")), D("0.136") * D("58.99") * D("1.07") + D("0.40"))

    def test_fee_includes_buyer_shipping_and_tax(self):
        self.assertEqual(fees(D("50"), D("6")), D("0.136") * D("56") * D("1.07") + D("0.40"))

    def test_small_order_fee_at_10_dollars_or_less(self):
        at_10 = D("10") / D("1.07")  # order total is exactly $10.00
        self.assertEqual(fees(at_10) - D("0.136") * D("10"), D("0.30"))
        self.assertEqual(fees(D("9.50")) - D("0.136") * D("9.50") * D("1.07"), D("0.40"))  # total 10.165

    def test_sneaker_8pct_tier_at_150_total(self):
        # 141 x 1.07 = 150.87 total: 8%, no per-order fee
        self.assertEqual(fees(D("141"), category=ATHLETIC_SHOES), D("0.08") * D("141") * D("1.07"))
        # 140 x 1.07 = 149.80 total: standard rate and per-order fee
        self.assertEqual(fees(D("140"), category=ATHLETIC_SHOES), D("0.136") * D("140") * D("1.07") + D("0.40"))
        # the same price in a standard category never gets 8%
        self.assertEqual(fees(D("141")), D("0.136") * D("141") * D("1.07") + D("0.40"))

    def test_category_detection(self):
        self.assertEqual(category_for("15709", ""), ATHLETIC_SHOES)
        self.assertEqual(category_for("", "Clothing, Shoes & Accessories:Women:Women's Shoes:Athletic Shoes"),
                         ATHLETIC_SHOES)
        self.assertEqual(category_for("52365", "Hats"), STANDARD)
        self.assertEqual(category_for(None, None), STANDARD)

    def test_fee_model_is_configurable(self):
        fm = FeeModel(fee_rate=D("0.10"), tax_rate=D("0"), per_order_fee=D("0"))
        self.assertEqual(break_even(D("90"), fm=fm), D("100.00"))


class BreakEvenTests(unittest.TestCase):
    def test_exact_break_even(self):
        be = break_even(D("50"))
        self.assertEqual(be, D("58.99"))
        self.assertGreaterEqual(net(be), D("50"))
        self.assertLess(net(be - D("0.01")), D("50"))

    def test_quick_estimate(self):
        self.assertEqual(quick_break_even(D("50")), D("59.50"))  # 50 x 1.17 + 1

    def test_shipping_and_label(self):
        # buyer pays 6.00, label costs 5.25
        be = break_even(D("50"), D("6.00"), D("5.25"))
        self.assertGreaterEqual(net(be, D("6.00"), D("5.25")), D("50"))
        self.assertLess(net(be - D("0.01"), D("6.00"), D("5.25")), D("50"))

    def test_sneaker_tier_break_even(self):
        self.assertEqual(break_even(D("140"), category=ATHLETIC_SHOES), D("153.11"))
        self.assertEqual(break_even(D("140")), D("164.32"))
        # cost 120: the cheapest covering price is the first cent of the 8% tier
        self.assertEqual(break_even(D("120"), category=ATHLETIC_SHOES), D("140.19"))

    def test_matches_brute_force_across_all_tiers(self):
        for cents in list(range(0, 1300, 11)) + list(range(11000, 15500, 97)):
            for ship, label in ((D("0"), D("0")), (D("4.50"), D("5.25"))):
                for cat in (STANDARD, ATHLETIC_SHOES):
                    cost = D(cents) / 100
                    self.assertEqual(break_even(cost, ship, label, cat),
                                     brute_force_break_even(cost, ship, label, cat), (cost, ship, label, cat))

    def test_rounds_up_never_down(self):
        self.assertEqual(ceil_cent(D("58.9801")), D("58.99"))
        self.assertEqual(ceil_cent(D("58.99")), D("58.99"))
        # 50.40 / 0.85448 = 58.9832...; half-up rounding would give 58.98, which loses money
        self.assertLess(net(D("58.98")), D("50"))

    def test_negative_inputs_rejected(self):
        with self.assertRaises(ValueError):
            break_even(D("-1"))

    def test_pricing_module_never_uses_float_or_the_clock(self):
        src = inspect.getsource(pricing)
        self.assertNotIn("float(", src)
        self.assertNotIn("date.today", src)
        self.assertNotIn("datetime.now", src)


class FloorTests(unittest.TestCase):
    def test_months_held_counts_full_months_only(self):
        self.assertEqual(months_held(date(2026, 4, 23), OCT), 5)
        self.assertEqual(months_held(date(2026, 4, 22), OCT), 6)
        self.assertEqual(months_held(date(2026, 12, 1), OCT), 0)

    def test_under_6_months_needs_profit(self):
        fr = compute_floor(rec(acquired=date(2026, 7, 1)), "Supreme Box Logo Tee", OCT, P)
        self.assertEqual(fr.floor, D("64.84"))  # break-even of 50 x 1.10
        self.assertGreaterEqual(net(fr.floor), D("55"))
        self.assertIn("under 6 months", fr.rule)

    def test_six_month_boundary(self):
        five = compute_floor(rec(acquired=date(2026, 4, 23)), "Supreme Camp Cap", OCT, P)   # 5 full months
        six = compute_floor(rec(acquired=date(2026, 4, 22)), "Supreme Camp Cap", OCT, P)    # exactly 6
        self.assertEqual((five.months_held, five.floor), (5, D("64.84")))
        self.assertEqual((six.months_held, six.floor), (6, D("58.99")))
        self.assertIn("6-12", six.rule)

    def test_6_to_12_months_break_even(self):
        fr = compute_floor(rec(acquired=date(2026, 1, 1)), "Supreme Camp Cap", OCT, P)
        self.assertEqual(fr.floor, D("58.99"))
        self.assertEqual(fr.break_even, D("58.99"))
        self.assertEqual(fr.quick_estimate, D("59.50"))

    def test_over_12_not_flagged_stays_break_even(self):
        fr = compute_floor(rec(acquired=date(2025, 6, 1)), "Supreme Camp Cap", OCT, P)
        self.assertEqual(fr.floor, D("58.99"))
        self.assertTrue(fr.notes)

    def test_over_12_flagged_allows_default_10pct_loss(self):
        fr = compute_floor(rec(acquired=date(2025, 6, 1), aged_ok=True), "Supreme Camp Cap", OCT, P)
        self.assertEqual(fr.floor, D("53.14"))  # break-even of 50 x 0.90
        self.assertGreaterEqual(net(fr.floor), D("45"))

    def test_loss_is_capped_at_20pct(self):
        fr = compute_floor(rec(acquired=date(2025, 6, 1), aged_ok=True, max_loss_pct=D("0.5")), "Cap", OCT, P)
        self.assertEqual(fr.floor, D("47.29"))  # break-even of 50 x 0.80, not 50 x 0.50
        self.assertIn("20%", fr.rule)

    def test_aged_flag_ignored_under_12_months(self):
        fr = compute_floor(rec(acquired=date(2026, 3, 1), aged_ok=True), "Cap", OCT, P)
        self.assertEqual(fr.floor, D("58.99"))
        self.assertTrue(any("ignored" in n for n in fr.notes))

    def test_winter_item_holds_for_profit_sep_to_dec(self):
        old = rec(acquired=date(2025, 1, 1), aged_ok=True)
        for title in ("Supreme Overdyed Beanie", "Supreme Balaclava", "Nike Thermal Top"):
            self.assertEqual(compute_floor(old, title, OCT, P).floor, D("64.84"), title)
        self.assertEqual(compute_floor(old, "Supreme Overdyed Beanie", date(2026, 9, 1), P).floor, D("64.84"))
        self.assertEqual(compute_floor(old, "Supreme Overdyed Beanie", date(2026, 12, 31), P).floor, D("64.84"))
        # out of season the normal age rules apply
        self.assertEqual(compute_floor(old, "Supreme Overdyed Beanie", FEB, P).floor, D("53.14"))
        self.assertEqual(compute_floor(old, "Supreme Overdyed Beanie", JUN, P).floor, D("53.14"))
        # a non-winter item in October is not held
        self.assertEqual(compute_floor(old, "Supreme Camp Cap", OCT, P).floor, D("53.14"))

    def test_floor_override_only_raises(self):
        self.assertEqual(compute_floor(rec(floor_override=D("80")), "Cap", OCT, P).floor, D("80.00"))
        low = compute_floor(rec(floor_override=D("10")), "Cap", OCT, P)
        self.assertEqual(low.floor, D("58.99"))
        self.assertTrue(any("override ignored" in n for n in low.notes))

    def test_sneaker_floor_uses_8pct_tier(self):
        fr = compute_floor(rec(cost="140"), "Nike SB Dunk Low", OCT, P, category=ATHLETIC_SHOES)
        self.assertEqual(fr.floor, D("153.11"))

    def test_policy_constants_are_editable(self):
        p = PricingPolicy(profit_pct=D("0.25"), winter_months=frozenset({1}))
        fr = compute_floor(rec(acquired=date(2026, 7, 1)), "Tee", OCT, p)
        self.assertEqual(fr.floor, break_even(D("62.50")))
        self.assertEqual(compute_floor(rec(), "Beanie", OCT, p).floor, D("58.99"))  # October no longer winter


class CheckTests(unittest.TestCase):
    fr = compute_floor(rec(), "Cap", OCT, P)  # floor 58.99

    def test_below_floor_blocked(self):
        self.assertTrue(check_price("58.98", "70", self.fr, P))
        self.assertEqual(check_price("58.99", "70", self.fr, P), [])

    def test_typo_guard(self):
        self.assertTrue(any("typo" in m for m in check_price("700", "70", self.fr, P)))
        self.assertEqual(check_price("210", "70", self.fr, P), [])       # exactly 3x is allowed
        self.assertTrue(check_price("210.01", "70", self.fr, P))

    def test_fractions_of_a_cent_rejected(self):
        self.assertTrue(check_price("62.555", "70", self.fr, P))

    def test_net_check_catches_the_10_dollar_fee_step(self):
        # cost 8.20: floor is 10.07, but the fee step can never let a higher price net less than cost
        fr = compute_floor(rec(cost="8.20"), "Cap", OCT, P)
        for cents in range(int(fr.floor * 100), 1300):
            price = D(cents) / 100
            if not check_price(price, "12", fr, P):
                self.assertGreaterEqual(net(price), D("8.20"))

    def test_offer_rules(self):
        self.assertEqual(check_offer("63", "70", self.fr, P), [])
        self.assertTrue(any("5" in m for m in check_offer("68", "70", self.fr, P)))  # only 2.9% off
        self.assertTrue(check_offer("50", "70", self.fr, P))
        self.assertEqual(check_offer("66.50", "70", self.fr, P), [])                 # exactly 5% off

    def test_discount_rounds_down_so_discount_is_never_smaller(self):
        self.assertEqual(offer_price_from_discount(D("69.99"), 5), D("66.49"))  # 66.4905 rounds down
        with self.assertRaises(ValueError):
            offer_price_from_discount(D("70"), 100)


class WeightTests(unittest.TestCase):
    def test_limits_by_title_keyword(self):
        self.assertEqual(weight_limit_lb("Supreme Camp Cap Black"), 2)
        self.assertEqual(weight_limit_lb("Supreme Overdyed Beanie"), 2)
        self.assertEqual(weight_limit_lb("Nike Thermal Top"), 4)
        self.assertEqual(weight_limit_lb("Supreme Gore-Tex Jacket"), 7)
        self.assertEqual(weight_limit_lb("Nike SB Dunk Low sneakers"), 8)
        self.assertEqual(weight_limit_lb("Mystery item"), 10)
        self.assertEqual(weight_limit_lb("Supreme Chatter Escape"), 10)  # whole words only: no "hat" or "cap"

    def test_over_limit_blocked_unless_allowed(self):
        self.assertTrue(check_weight("Supreme Camp Cap", 25, 0))
        self.assertTrue(check_weight("Supreme Camp Cap", 2, 1))
        self.assertEqual(check_weight("Supreme Camp Cap", 2, 0), [])
        self.assertEqual(check_weight("Supreme Camp Cap", 25, 0, allow_unusual=True), [])
        self.assertTrue(check_weight("Nike Dunk", 8, 1))
        self.assertEqual(check_weight("Nike Dunk", 8, 0), [])

    def test_nonsense_weights_blocked_even_when_allowed(self):
        self.assertTrue(check_weight("Cap", 0, 0, allow_unusual=True))
        self.assertTrue(check_weight("Cap", 0, 16, allow_unusual=True))
        self.assertTrue(check_weight("Cap", -1, 3, allow_unusual=True))


ROWS = [
    # A date, B item, C size, D colorway, E subtotal, F ship, G tax, H payment, I total, J status, K..U, V, W, X
    ["10/01/2025", "Supreme Beanie", "OS", "Black", 40, 5, 3, "Visa", 48, "Listed",
     "", "", "", "", "", "", "", "", "", "", "", "123456789012", "Y", ""],
    ["10/01/2025", "Supreme Beanie", "OS", "Black", 42, 5, 3, "Visa", "$50.00", "Listed",
     "", "", "", "", "", "", "", "", "", "", "", 123456789012.0, "", 75],
    ["11/01/2025", "No item id yet", "", "", 10, 0, 0, "", 10, "Inventory"],
    ["bad date", "Thing", "", "", 1, 0, 0, "", 1, "Listed", "", "", "", "", "", "", "", "", "", "", "",
     "999999999999"],
    [45931, "Serial date", "", "", 1, 0, 0, "", D("19.99"), "Listed", "", "", "", "", "", "", "", "", "", "", "",
     "888888888888"],
]


class CostSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_rows_to_records(self):
        recs = records_from_rows(ROWS, item_col="V", aged_col="W", floor_col="X")
        self.assertEqual(sorted(recs), ["123456789012", "888888888888"])
        r = recs["123456789012"]
        self.assertEqual(r.cost, D("50.00"))  # same item on two rows: the highest cost wins
        self.assertEqual(r.sheet_row, 3)
        self.assertEqual(r.floor_override, D("75"))
        self.assertFalse(r.aged_ok)
        self.assertEqual(r.acquired, date(2025, 10, 1))
        self.assertEqual(recs["888888888888"].acquired, date(2025, 10, 1))  # Sheets serial 45931
        self.assertEqual(recs["888888888888"].cost, D("19.99"))

    def test_configurable_columns(self):
        row = ["987654321098", "y", "", "2025-10-01", "Cap", "33.10"]
        recs = records_from_rows([row], item_col="A", aged_col="B", floor_col="C", date_col="D", title_col="E",
                                 cost_col="F", status_col="E")
        self.assertEqual(recs["987654321098"].cost, D("33.10"))
        self.assertTrue(recs["987654321098"].aged_ok)

    def test_parsers(self):
        self.assertEqual(parse_item_id(123456789012.0), "123456789012")
        self.assertEqual(parse_item_id(D("123456789012")), "123456789012")
        self.assertIsNone(parse_item_id(D("12345678901.5")))
        self.assertEqual(parse_date(D("45931")), date(2025, 10, 1))
        self.assertIsNone(parse_item_id("123</ItemID>"))
        self.assertIsNone(parse_item_id(True))
        self.assertEqual(parse_date("10/1/2025"), date(2025, 10, 1))
        self.assertIsNone(parse_date("soon"))

    async def test_json_source(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"items": {"123456789012": {"cost": 52.30, "acquired": "2026-03-01", "aged_ok": true},'
                    ' "223456789012": {"cost": "oops", "acquired": "2026-03-01"}}}')
        src = JsonCostSource(f.name)
        r = await src.get("123456789012")
        self.assertEqual(r.cost, D("52.30"))
        self.assertIsInstance(r.cost, D)
        self.assertIsNone(await src.get("000000000000"))
        with self.assertRaises(CostSourceError):
            await src.get("223456789012")

    async def test_sheets_source_reads_with_readonly_scope_and_caches_5_minutes(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"client_email": "reader@proj.iam.gserviceaccount.com", "private_key": pem,
                       "token_uri": "https://oauth2.googleapis.com/token"}, f)
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            if req.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "ya29.google", "expires_in": 3600})
            return httpx.Response(200, content=json.dumps({"values": [
                [45931, "Supreme Beanie", "OS", "Black", 40, 5, 3, "Visa", 48.1, "Listed",
                 "", "", "", "", "", "", "", "", "", "", "", 123456789012, "Y"]]}).encode())

        now = [time.time()]
        src = SheetsCostSource("sheet-id-1234567890", "Lucas Resell", f.name, item_col="V", aged_col="W",
                               floor_col="X", transport=httpx.MockTransport(handler), clock=lambda: now[0])
        r = await src.get("123456789012")
        self.assertEqual((r.cost, r.aged_ok, r.acquired), (D("48.1"), True, date(2025, 10, 1)))
        self.assertIsNone(await src.get("555555555555"))
        self.assertEqual(len(seen), 2)  # one token call, one sheet read, then cache
        assertion = dict(p.split("=") for p in seen[0].content.decode().split("&"))["assertion"]
        claims = jwt.decode(assertion, options={"verify_signature": False})
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/spreadsheets.readonly")
        self.assertIn("Lucas%20Resell", str(seen[1].url))
        self.assertIn("A2%3AX", str(seen[1].url))
        self.assertEqual(seen[1].headers["Authorization"], "Bearer ya29.google")
        now[0] += 301
        await src.get("123456789012")
        self.assertEqual(len(seen), 3)  # cache expired after 5 minutes: sheet read again, token reused

    async def test_sheets_error_has_no_secrets_and_fails_closed(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"client_email": "r@p.iam.gserviceaccount.com", "private_key": pem,
                       "token_uri": "https://oauth2.googleapis.com/token"}, f)

        def handler(req):
            if req.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "ya29.google", "expires_in": 3600})
            return httpx.Response(403, json={"error": "denied"})

        src = SheetsCostSource("sheet-id-1234567890", "Lucas Resell", f.name, transport=httpx.MockTransport(handler))
        with self.assertRaises(CostSourceError) as cm:
            await src.get("123456789012")
        self.assertNotIn("ya29", str(cm.exception))
        self.assertIn("Viewer", str(cm.exception))


class StoreTests(unittest.TestCase):
    def test_pending_single_use_and_expiry(self):
        now = [1000.0]
        st = Store(clock=lambda: now[0])
        cid, expires = st.create_pending("price", {"items": []})
        self.assertEqual(expires, 1000.0 + 15 * 60)
        st.claim_pending(cid)
        with self.assertRaises(LookupError):
            st.claim_pending(cid)
        cid2, _ = st.create_pending("price", {"items": []})
        now[0] += 15 * 60 + 1
        with self.assertRaises(LookupError):
            st.claim_pending(cid2)

    def test_change_ids_are_random(self):
        st = Store()
        ids = {st.create_pending("price", {})[0] for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) >= 24 for i in ids))
        with self.assertRaises(LookupError):
            st.claim_pending("chg_' OR 1=1 --")

    def test_audit_roundtrip(self):
        st = Store()
        aid = st.log(change_id="c", kind="price", item_id="1", field="price", old="70.00", new="60.00",
                     reason="markdown", status="applied")
        row = st.get_audit(aid)
        self.assertEqual(row["old_value"], "70.00")
        self.assertEqual(row["change_id"], "c")
        self.assertTrue(row["ts"].endswith("+00:00"))  # UTC timestamp
        self.assertEqual(len(st.list_audit("1")), 1)


if __name__ == "__main__":
    unittest.main()
