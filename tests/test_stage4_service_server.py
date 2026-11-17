import json
import time
import unittest
from datetime import date
from decimal import Decimal

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp.server.mcpserver.exceptions import ToolError

from ebay_mcp.remote import JwtVerifier
from ebay_mcp.server import build_server
from ebay_mcp.service import GuardrailError

from ebay_mcp.config import ConfigError
from ebay_mcp.pricing import CostRecord
from ebay_mcp.remote import remote_config

from fakes import BEANIE, CAP, NO_COST, make_service
from helpers import make_settings


class PriceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_does_not_write(self):
        svc, t, *_ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62, "reason": "markdown"}])
        self.assertTrue(out["preview_only"])
        self.assertIn("change_id", out)
        self.assertEqual(out["will_change"][0]["floor"], "58.99")
        self.assertEqual(out["will_change"][0]["cost"], "50.00")
        self.assertRegex(out["change_id"], r"^chg_[\w-]{20,}$")
        self.assertEqual(t.price_calls, [])

    async def test_below_floor_and_unknown_cost_blocked(self):
        svc, *_ = make_service()
        out = await svc.preview_price_changes([
            {"item_id": CAP, "new_price": 55},
            {"item_id": NO_COST, "new_price": 120},
        ])
        self.assertNotIn("change_id", out)
        self.assertEqual(len(out["blocked"]), 2)

    async def test_winter_item_holds_profit_in_october(self):
        svc, *_ = make_service()
        out = await svc.preview_price_changes([{"item_id": BEANIE, "new_price": 45}])
        self.assertIn("winter", out["blocked"][0]["problems"][0])

    async def test_confirm_applies_logs_and_is_single_use(self):
        svc, t, _, _, store = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62, "reason": "6 mo markdown"}])
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual(t.items[CAP]["price"], "62.00")
        log = store.list_audit(CAP)[0]
        self.assertEqual((log["old_value"], log["new_value"], log["reason"]), ("70.00", "62.00", "6 mo markdown"))
        self.assertEqual(res["applied"][0]["audit_id"], log["id"])
        self.assertEqual((log["item_id"], log["status"], log["change_id"]), (CAP, "applied", out["change_id"]))
        self.assertTrue(log["ts"].endswith("+00:00"))
        with self.assertRaises(LookupError):
            await svc.confirm_change(out["change_id"])
        self.assertEqual(len(t.price_calls), 1)  # the second confirm wrote nothing

    async def test_expired_change_id_refused(self):
        now = [1000.0]
        svc, t, *_ = make_service(clock=lambda: now[0])
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        now[0] += 15 * 60 + 1
        with self.assertRaises(LookupError) as cm:
            await svc.confirm_change(out["change_id"])
        self.assertIn("expired", str(cm.exception))
        self.assertEqual(t.price_calls, [])

    async def test_confirm_rechecks_rules_against_fresh_cost(self):
        svc, t, _, costs, store = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        costs.recs[CAP] = CostRecord(item_id=CAP, cost=Decimal("60"), acquired=date(2026, 1, 10))  # sheet corrected
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual((res["applied"], t.price_calls), ([], []))
        self.assertIn("below the floor", res["skipped"][0]["problems"][0])
        self.assertEqual(store.list_audit(CAP)[0]["status"], "skipped")

    async def test_confirm_blocks_if_cost_record_disappears(self):
        svc, t, _, costs, _ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        del costs.recs[CAP]
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual((res["applied"], t.price_calls), ([], []))

    async def test_cost_cannot_be_supplied_by_the_caller(self):
        svc, *_ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 30, "cost": 1, "floor": 1}])
        self.assertNotIn("change_id", out)
        self.assertIn("below the floor", out["blocked"][0]["problems"][0])
        schema = (await build_server(svc).list_tools())
        for tool in schema:
            self.assertNotIn("cost", json.dumps(tool.input_schema).lower(), tool.name)

    async def test_typo_guard_in_preview(self):
        svc, *_ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 700}])
        self.assertTrue(any("typo" in p for p in out["blocked"][0]["problems"]))

    async def test_batches_of_more_than_4_are_split(self):
        svc, t, _, costs, _ = make_service()
        ids = [str(400000000000 + i) for i in range(6)]
        for i in ids:
            t.items[i] = {**t.items[CAP], "item_id": i}
            costs.recs[i] = CostRecord(item_id=i, cost=Decimal("50"), acquired=date(2026, 1, 10))
        out = await svc.preview_price_changes([{"item_id": i, "new_price": 62} for i in ids])
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual([len(c) for c in t.price_calls], [4, 2])
        self.assertEqual(len(res["applied"]), 6)

    async def test_drift_since_preview_aborts(self):
        svc, t, *_ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        t.items[CAP]["price"] = "68.00"  # Lucas changed it in the app meanwhile
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual(res["applied"], [])
        self.assertIn("changed", res["skipped"][0]["problems"][0])
        self.assertEqual(t.price_calls, [])
        self.assertEqual(t.items[CAP]["price"], "68.00")

    async def test_kill_switch(self):
        svc, t, *_ = make_service(writes=False)
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        self.assertFalse(out["writes_enabled"])
        with self.assertRaises(GuardrailError):
            await svc.confirm_change(out["change_id"])
        self.assertEqual(t.price_calls, [])

    async def test_ebay_failure_logged(self):
        svc, t, _, _, store = make_service()
        t.fail_prices = True
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(store.list_audit(CAP)[0]["status"], "failed")

    async def test_undo_restores_old_price(self):
        svc, t, _, _, store = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}])
        aid = (await svc.confirm_change(out["change_id"]))["applied"][0]["audit_id"]
        undo = await svc.preview_undo(aid)
        self.assertEqual(undo["will_change"][0]["new_price"], "70.00")
        await svc.confirm_change(undo["change_id"])
        self.assertEqual(t.items[CAP]["price"], "70.00")
        self.assertIsNotNone(store.get_audit(aid)["undone_by"])
        with self.assertRaises(GuardrailError):
            await svc.preview_undo(aid)
        self.assertEqual(store.list_audit(CAP)[0]["reason"], f"undo audit #{aid}")

    async def test_undo_obeys_the_floor(self):
        svc, t, _, costs, _ = make_service()
        out = await svc.preview_price_changes([{"item_id": CAP, "new_price": 75}])
        aid = (await svc.confirm_change(out["change_id"]))["applied"][0]["audit_id"]
        costs.recs[CAP] = CostRecord(item_id=CAP, cost=Decimal("62"), acquired=date(2026, 1, 10))
        undo = await svc.preview_undo(aid)  # going back to 70.00 is now below the floor
        self.assertNotIn("change_id", undo)

    async def test_batch_limits(self):
        svc, *_ = make_service()
        with self.assertRaises(GuardrailError):
            await svc.preview_price_changes([{"item_id": CAP, "new_price": 62}] * 2)


class OfferAndPackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_preview_and_send(self):
        svc, _, r, *_ = make_service()
        out = await svc.preview_offer(CAP, discount_pct=10, message="Thanks for watching!")
        self.assertEqual(out["will_change"][0]["offer_price"], "63.00")
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual(res["applied"][0]["offers_sent"], 3)
        self.assertEqual(len(r.offers), 1)
        with self.assertRaises(GuardrailError):  # offers cannot be undone
            await svc.preview_undo(res["applied"][0]["audit_id"])

    async def test_offer_needs_cost_and_is_skipped_on_drift(self):
        svc, t, r, _, store = make_service()
        r.eligible = {CAP, NO_COST}
        blocked = await svc.preview_offer(NO_COST, discount_pct=10)
        self.assertNotIn("change_id", blocked)
        out = await svc.preview_offer(CAP, discount_pct=10)
        t.items[CAP]["price"] = "75.00"
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual((res["applied"], r.offers), ([], []))
        self.assertEqual(store.list_audit(CAP)[0]["status"], "skipped")

    async def test_failed_offer_and_package_writes_are_logged(self):
        svc, t, r, _, store = make_service()
        r.fail_offer, t.fail_package = True, True
        out = await svc.preview_offer(CAP, discount_pct=10)
        res = await svc.confirm_change(out["change_id"])
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual((store.list_audit(CAP)[0]["kind"], store.list_audit(CAP)[0]["status"]), ("offer", "failed"))
        out = await svc.preview_package_update(CAP, 0, 9)
        res = await svc.confirm_change(out["change_id"])
        log = store.list_audit(CAP)[0]
        self.assertEqual((log["kind"], log["status"]), ("package", "failed"))
        self.assertNotIn("abc.def", log["detail"] + str(res))  # token redacted in log and output

    async def test_package_needs_cost_record_and_fixed_values(self):
        svc, *_ = make_service()
        out = await svc.preview_package_update(NO_COST, 3, 0)
        self.assertTrue(any("No cost record" in p for p in out["blocked"][0]["problems"]))
        out = await svc.preview_package_update(CAP, 0, 9, package_type="<Evil/>")
        self.assertNotIn("change_id", out)
        out = await svc.preview_package_update(CAP, 0, 9, length_in=99)
        self.assertNotIn("change_id", out)
        out = await svc.preview_package_update(CAP, 18, 0, allow_unusual_weight=True)
        self.assertIn("change_id", out)

    async def test_package_undo_restores_old_weight(self):
        svc, t, _, _, store = make_service()
        good = await svc.preview_package_update(CAP, 0, 9)
        self.assertEqual(good["will_change"][0]["new_package"]["length_in"], 10)  # kept from the listing
        aid = (await svc.confirm_change(good["change_id"]))["applied"][0]["audit_id"]
        undo = await svc.preview_undo(aid)
        await svc.confirm_change(undo["change_id"])
        self.assertEqual(t.items[CAP]["package"]["weight_lb"], 25)
        self.assertIsNotNone(store.get_audit(aid)["undone_by"])

    async def test_offer_blocked_below_floor_or_no_watchers(self):
        svc, _, r, *_ = make_service()
        self.assertNotIn("change_id", await svc.preview_offer(CAP, offer_price=55))
        r.eligible = set()
        out = await svc.preview_offer(CAP, offer_price=63)
        self.assertTrue(any("interested" in p for p in out["blocked"][0]["problems"]))

    async def test_package_sanity_and_fix(self):
        svc, t, *_ = make_service()
        bad = await svc.preview_package_update(CAP, 18, 0)
        self.assertTrue(any("sanity" in p for p in bad["blocked"][0]["problems"]))
        good = await svc.preview_package_update(CAP, 0, 9, 10, 8, 2, "PackageThickEnvelope")
        self.assertEqual(good["will_change"][0]["old_package"]["weight_lb"], 25)
        await svc.confirm_change(good["change_id"])
        self.assertEqual(t.package_calls, [(CAP, 0, 9)])


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_listings_has_floor_and_weight(self):
        svc, *_ = make_service()
        out = await svc.list_listings()
        self.assertEqual(out["total_active"], 3)
        rows = {r["item_id"]: r for r in out["listings"]}
        self.assertEqual(rows[CAP]["pricing"]["floor"], "58.99")
        self.assertEqual(rows[CAP]["pricing"]["cost"], "50.00")
        self.assertEqual((rows[CAP]["price"], rows[CAP]["watchers"]), ("70.00", 4))
        page = await svc.list_listings(limit=1, offset=1)
        self.assertEqual((page["returned"], page["next_offset"]), (1, 2))
        self.assertEqual(rows[CAP]["package"]["weight_lb"], 25)
        self.assertIn("blocked", rows[NO_COST]["pricing"])
        self.assertIn("days_listed", rows[CAP])

    async def test_order_fees_sheet_values(self):
        svc, *_ = make_service()
        out = await svc.get_order_fees("12-34567-89012")
        self.assertEqual(out["sheet_values"], {
            "K_sale_date": "2026-10-20", "L_platform": "eBay", "M_sold_price": "75.60",  # price + tax
            "N_transaction_fees": "10.97", "O_tax": "5.60", "P_shipping_paid": "6.25",
            "Q_payment_processing": "0.00", "R_label_cost": "5.49"})
        self.assertEqual(out["fees"]["fee_breakdown"]["FINAL_VALUE_FEE"], "10.57")

    async def test_pricing_floor_tool_output(self):
        svc, *_ = make_service()
        out = await svc.get_pricing_floor(CAP)
        self.assertEqual((out["cost"], out["break_even"], out["quick_estimate"], out["floor"]),
                         ("50.00", "58.99", "59.50", "58.99"))
        self.assertIn("6-12 months", out["rule"])
        self.assertIn("blocked", await svc.get_pricing_floor(NO_COST))

    async def test_comps_stats(self):
        svc, *_ = make_service()
        out = await svc.search_comps("supreme camp cap")
        self.assertEqual(out["stats"], {"count": 3, "min_delivered": "65.00", "median_delivered": "75.00",
                                        "max_delivered": "90.00"})
        self.assertEqual(out["price_type"], "asking")
        self.assertIn("not sold prices", out["note"])


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_annotations(self):
        svc, *_ = make_service()
        server = build_server(svc)
        tools = {t.name: t for t in await server.list_tools()}
        self.assertEqual(set(tools), {
            "list_listings", "get_pricing_floor", "update_price", "send_offer_to_watchers",
            "update_package_weight", "confirm_change", "undo_change", "recent_changes",
            "get_orders", "get_order_fees", "search_comps"})
        for name, tool in tools.items():
            a = tool.annotations
            if name == "confirm_change":  # the only write tool
                self.assertTrue(a.destructive_hint)
                self.assertFalse(a.read_only_hint)
            else:
                self.assertTrue(a.read_only_hint, name)
                self.assertFalse(a.destructive_hint, name)
        changes = tools["update_price"].input_schema["properties"]["changes"]
        self.assertEqual((changes["minItems"], changes["maxItems"]), (1, 20))
        for name in ("update_price", "send_offer_to_watchers", "update_package_weight", "undo_change"):
            self.assertIn("PREVIEW ONLY", tools[name].description)
        self.assertIn("explicit yes", server.instructions)

    async def test_update_price_rejects_more_than_20(self):
        svc, *_ = make_service()
        server = build_server(svc)
        with self.assertRaises(ToolError):
            await server.call_tool("update_price", {"changes": [{"item_id": CAP, "new_price": 62}] * 21})
        with self.assertRaises(ToolError):
            await server.call_tool("update_price", {"changes": []})

    async def test_kill_switch_through_the_tool(self):
        svc, t, *_ = make_service(writes=False)
        server = build_server(svc)
        res = await server.call_tool("update_price", {"changes": [{"item_id": CAP, "new_price": 62}]})
        change_id = json.loads(res.content[0].text)["change_id"]
        with self.assertRaises(ToolError) as cm:
            await server.call_tool("confirm_change", {"change_id": change_id})
        self.assertIn("Writes are disabled", str(cm.exception))
        self.assertEqual(t.price_calls, [])

    async def test_update_price_tool_returns_preview(self):
        svc, t, *_ = make_service()
        server = build_server(svc)
        res = await server.call_tool("update_price", {"changes": [{"item_id": CAP, "new_price": 62}]})
        self.assertFalse(res.is_error)
        body = json.loads(res.content[0].text)
        self.assertTrue(body["preview_only"])
        self.assertEqual(t.price_calls, [])

    async def test_errors_become_tool_errors(self):
        svc, *_ = make_service()
        server = build_server(svc)
        # In-process call_tool raises ToolError; over the wire the SDK sends it as an is_error result.
        with self.assertRaises(ToolError) as cm:
            await server.call_tool("confirm_change", {"change_id": "chg_nope"})
        self.assertIn("Unknown change_id", str(cm.exception))


class FakeJwks:
    def __init__(self, pub):
        self.pub = pub

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.pub})()


class RemoteAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.v = JwtVerifier("https://idp.example.com/", "https://ebay-mcp.example.com/mcp", "unused",
                             {"user|lucas"}, jwks_client=FakeJwks(self.key.public_key()))

    def tok(self, **over):
        now = int(time.time())
        claims = {"iss": "https://idp.example.com/", "aud": "https://ebay-mcp.example.com/mcp",
                  "sub": "user|lucas", "iat": now, "exp": now + 600}
        claims.update(over)
        return jwt.encode(claims, self.key, algorithm="RS256")

    async def test_accepts_owner(self):
        self.assertIsNotNone(await self.v.verify_token(self.tok()))

    async def test_rejects_other_user_wrong_audience_and_expired(self):
        self.assertIsNone(await self.v.verify_token(self.tok(sub="user|attacker")))
        self.assertIsNone(await self.v.verify_token(self.tok(aud="https://other.example.com/mcp")))
        self.assertIsNone(await self.v.verify_token(self.tok(exp=int(time.time()) - 600)))

    async def test_rejects_alg_none(self):
        now = int(time.time())
        unsigned = jwt.encode({"iss": "https://idp.example.com/", "aud": "https://ebay-mcp.example.com/mcp",
                               "sub": "user|lucas", "iat": now, "exp": now + 600}, None, algorithm="none")
        self.assertIsNone(await self.v.verify_token(unsigned))

    async def test_rejects_wrong_issuer_wrong_key_hmac_and_garbage(self):
        self.assertIsNone(await self.v.verify_token(self.tok(iss="https://evil.example.com/")))
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        claims = {"iss": "https://idp.example.com/", "aud": "https://ebay-mcp.example.com/mcp",
                  "sub": "user|lucas", "iat": now, "exp": now + 600}
        self.assertIsNone(await self.v.verify_token(jwt.encode(claims, other, algorithm="RS256")))
        self.assertIsNone(await self.v.verify_token(jwt.encode(claims, "x" * 40, algorithm="HS256")))
        self.assertIsNone(await self.v.verify_token("not-a-jwt"))
        no_exp = {k: v for k, v in claims.items() if k != "exp"}
        self.assertIsNone(await self.v.verify_token(jwt.encode(no_exp, self.key, algorithm="RS256")))

    async def test_accepted_token_carries_subject_and_audience(self):
        tok = await self.v.verify_token(self.tok(aud=["https://ebay-mcp.example.com/mcp", "https://idp/userinfo"]))
        self.assertEqual((tok.subject, tok.resource), ("user|lucas", "https://ebay-mcp.example.com/mcp"))


REMOTE = {"EBAY_MCP_TRANSPORT": "streamable-http", "EBAY_MCP_PUBLIC_URL": "https://ebay-mcp.example.com/mcp",
          "EBAY_MCP_OAUTH_ISSUER": "https://idp.example.com/",
          "EBAY_MCP_JWKS_URL": "https://idp.example.com/.well-known/jwks.json",
          "EBAY_MCP_ALLOWED_SUBJECTS": "user|lucas"}


class RemoteStartupTests(unittest.IsolatedAsyncioTestCase):
    def test_refuses_to_start_without_oauth(self):
        from ebay_mcp.remote import run_http
        for missing in ("EBAY_MCP_PUBLIC_URL", "EBAY_MCP_OAUTH_ISSUER", "EBAY_MCP_JWKS_URL",
                        "EBAY_MCP_ALLOWED_SUBJECTS"):
            env = {k: v for k, v in REMOTE.items() if k != missing}
            built = []
            with self.assertRaises(ConfigError, msg=missing) as cm:
                run_http(make_settings(**env), lambda s: built.append(s), build_server)
            self.assertIn(missing, str(cm.exception))
            self.assertEqual(built, [])  # refused before the service was even built

    def test_refuses_plain_http_urls(self):
        for key in ("EBAY_MCP_PUBLIC_URL", "EBAY_MCP_OAUTH_ISSUER", "EBAY_MCP_JWKS_URL"):
            with self.assertRaises(ConfigError, msg=key):
                remote_config(make_settings(**{**REMOTE, key: "http://insecure.example.com/x"}))
        with self.assertRaises(ConfigError):
            remote_config(make_settings(**{**REMOTE, "EBAY_MCP_JWKS_URL": "file:///etc/passwd"}))

    def test_valid_config_enables_dns_rebinding_protection(self):
        cfg = remote_config(make_settings(**REMOTE))
        self.assertTrue(cfg["security"].enable_dns_rebinding_protection)
        self.assertEqual(cfg["security"].allowed_hosts, ["ebay-mcp.example.com"])
        self.assertEqual(cfg["path"], "/mcp")
        self.assertEqual(cfg["server"]["token_verifier"].audience, "https://ebay-mcp.example.com/mcp")

    def test_http_app_rejects_missing_token_and_foreign_host(self):
        from starlette.testclient import TestClient
        cfg = remote_config(make_settings(**REMOTE))
        svc, *_ = make_service()
        app = build_server(svc, **cfg["server"]).streamable_http_app(
            stateless_http=True, streamable_http_path=cfg["path"], transport_security=cfg["security"])
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"Accept": "application/json, text/event-stream"}
        with TestClient(app, base_url="https://ebay-mcp.example.com") as c:
            self.assertEqual(c.post("/mcp", json=body, headers=headers).status_code, 401)
            bad = c.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer not-a-jwt"})
            self.assertEqual(bad.status_code, 401)


if __name__ == "__main__":
    unittest.main()
