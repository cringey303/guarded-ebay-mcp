import os
import tempfile
import unittest

import httpx2 as httpx

from ebay_mcp.auth import AuthError, TokenManager, build_consent_url, exchange_code, state_matches
from ebay_mcp.config import ConfigError, Settings, load_env_file
from ebay_mcp.http import EbayHttp, RateLimitExceeded, redact
from ebay_mcp.store import Store

from helpers import ENV, Recorder, is_token, json_resp, make_settings, token_ok


async def no_sleep(_):
    return None


class ConfigTests(unittest.TestCase):
    def test_missing_required(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"EBAY_ENV": "sandbox"})

    def test_secrets_never_in_repr(self):
        s = make_settings()
        text = repr(s)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("refresh-secret", text)

    def test_writes_disabled_by_default(self):
        env = {k: v for k, v in ENV.items() if k != "EBAY_MCP_WRITES_ENABLED"}
        self.assertFalse(Settings.from_env(env).writes_enabled)

    @unittest.skipUnless(os.name == "posix", "permission check is POSIX only")
    def test_env_file_permission_check(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("EBAY_CLIENT_ID=x   # app id\nEBAY_CLIENT_SECRET='a#b'\n# comment\n")
        os.chmod(f.name, 0o644)
        with self.assertRaises(ConfigError):
            load_env_file(f.name, {})
        os.chmod(f.name, 0o600)
        loaded = load_env_file(f.name, {})
        self.assertEqual(loaded["EBAY_CLIENT_ID"], "x")
        self.assertEqual(loaded["EBAY_CLIENT_SECRET"], "a#b")
        os.unlink(f.name)

    def test_env_file_last_line_wins_and_real_env_overrides(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("\ufeffEBAY_REFRESH_TOKEN=\nEBAY_CLIENT_ID=from-file\nEBAY_REFRESH_TOKEN=new-token\n")
        os.chmod(f.name, 0o600)
        loaded = load_env_file(f.name, {"EBAY_CLIENT_ID": "from-env"})
        self.assertEqual(loaded["EBAY_REFRESH_TOKEN"], "new-token")  # the blank template line does not win
        self.assertEqual(loaded["EBAY_CLIENT_ID"], "from-env")
        os.unlink(f.name)

    def test_bad_environment_rejected(self):
        with self.assertRaises(ConfigError):
            make_settings(EBAY_ENV="prod")
        self.assertEqual(make_settings(EBAY_ENV="production").urls["apiz"], "https://apiz.ebay.com")

    def test_consent_url_has_state_and_least_privilege_scopes(self):
        url, state = build_consent_url(make_settings())
        self.assertIn("auth.sandbox.ebay.com", url)
        self.assertIn(state, url)
        self.assertGreaterEqual(len(state), 32)
        from urllib.parse import parse_qs, urlparse
        scopes = parse_qs(urlparse(url).query)["scope"][0].split()
        base = "https://api.ebay.com/oauth/api_scope"
        self.assertEqual(scopes, [base, base + "/sell.inventory", base + "/sell.fulfillment.readonly",
                                  base + "/sell.finances"])

    def test_state_is_verified(self):
        _, state = build_consent_url(make_settings())
        self.assertTrue(state_matches(state, state))
        self.assertFalse(state_matches(state, "attacker-state"))
        self.assertFalse(state_matches(state, ""))
        self.assertFalse(state_matches("", ""))

    @unittest.skipUnless(os.name == "posix", "permission check is POSIX only")
    def test_refresh_token_saved_by_replacing_the_line(self):
        import importlib.util
        import pathlib
        spec = importlib.util.spec_from_file_location(
            "get_refresh_token", pathlib.Path(__file__).parents[1] / "scripts" / "get_refresh_token.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("EBAY_CLIENT_ID=x\nEBAY_REFRESH_TOKEN=\n")
        path = pathlib.Path(f.name)
        mod.save_refresh_token(path, "v^1.1#new")
        mod.save_refresh_token(path, "v^1.1#newer")
        self.assertEqual(path.read_text().count("EBAY_REFRESH_TOKEN="), 1)
        self.assertEqual(load_env_file(path, {})["EBAY_REFRESH_TOKEN"], "v^1.1#newer")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.unlink()


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_once_then_cache(self):
        rec = Recorder([(is_token, token_ok)])
        now = [1000.0]
        tm = TokenManager(make_settings(), transport=rec.transport(), clock=lambda: now[0])
        self.assertEqual(await tm.user_token(), "v^1.1#access-123")
        await tm.user_token()
        self.assertEqual(len(rec.requests), 1)
        body = rec.requests[0].content.decode()
        self.assertIn("grant_type=refresh_token", body)
        self.assertTrue(rec.requests[0].headers["Authorization"].startswith("Basic "))
        now[0] += 7200 - 200  # inside the 5-minute refresh margin
        await tm.user_token()
        self.assertEqual(len(rec.requests), 2)

    async def test_app_token_is_separate_and_uses_client_credentials(self):
        rec = Recorder([(is_token, token_ok)])
        tm = TokenManager(make_settings(), transport=rec.transport())
        await tm.app_token()
        await tm.app_token()
        await tm.user_token()
        self.assertEqual(len(rec.requests), 2)
        self.assertIn("grant_type=client_credentials", rec.requests[0].content.decode())
        self.assertNotIn("refresh_token", rec.requests[0].content.decode())

    async def test_code_exchange_sends_code_and_runame(self):
        rec = Recorder([(is_token, json_resp({"access_token": "a", "refresh_token": "v^1.1#r",
                                              "refresh_token_expires_in": 47304000}))])
        out = await exchange_code(make_settings(), "the-code", transport=rec.transport())
        self.assertEqual(out["refresh_token"], "v^1.1#r")
        body = rec.requests[0].content.decode()
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("code=the-code", body)
        self.assertIn("redirect_uri=Lucas_Root-LucasRoo-resale-abcdef", body)

    async def test_invalid_grant_message_has_no_secret(self):
        rec = Recorder([(is_token, json_resp({"error": "invalid_grant"}, 400))])
        tm = TokenManager(make_settings(), transport=rec.transport())
        with self.assertRaises(AuthError) as cm:
            await tm.user_token()
        self.assertIn("get_refresh_token", str(cm.exception))
        self.assertNotIn("refresh-secret", str(cm.exception))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def _http(self, routes, **settings_kw):
        rec = Recorder([(is_token, token_ok)] + routes)
        s = make_settings()
        for k, v in settings_kw.items():
            setattr(s, k, v)
        s.requests_per_second = 1000
        tm = TokenManager(s, transport=rec.transport())
        return EbayHttp(s, tm, Store(), transport=rec.transport(), sleep=no_sleep), rec

    async def test_retries_429_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req):
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"}) if calls["n"] == 1 else httpx.Response(200, json={})

        http, rec = self._http([(lambda r: "/sell/" in r.url.path, flaky)])
        resp = await http.request("fulfillment", "GET", "https://api.sandbox.ebay.com/sell/fulfillment/v1/order")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 2)

    async def test_401_invalidates_token_and_retries_once(self):
        calls = {"n": 0}

        def expired(req):
            calls["n"] += 1
            return httpx.Response(401) if calls["n"] == 1 else httpx.Response(200, json={})

        http, rec = self._http([(lambda r: "/sell/" in r.url.path, expired)])
        resp = await http.request("fulfillment", "GET", "https://api.sandbox.ebay.com/sell/fulfillment/v1/order")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sum(1 for r in rec.requests if is_token(r)), 2)

    async def test_retries_5xx_with_backoff_then_gives_up(self):
        delays = []

        async def record_sleep(d):
            delays.append(d)

        rec = Recorder([(is_token, token_ok), (lambda r: True, lambda r: httpx.Response(503))])
        s = make_settings()
        s.requests_per_second = 0
        http = EbayHttp(s, TokenManager(s, transport=rec.transport()), Store(), transport=rec.transport(),
                        sleep=record_sleep)
        resp = await http.request("fulfillment", "GET", "https://api.sandbox.ebay.com/sell/fulfillment/v1/order")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(len(delays), 3)                 # 4 attempts, 3 waits
        self.assertTrue(delays[0] < delays[1] < delays[2])  # exponential backoff

    async def test_writes_are_not_resent_on_5xx_but_are_on_429(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(500)

        http, _ = self._http([(lambda r: "/sell/" in r.url.path, handler)])
        url = "https://api.sandbox.ebay.com/sell/negotiation/v1/send_offer_to_interested_buyers"
        resp = await http.request("negotiation", "POST", url, idempotent=False)
        self.assertEqual((resp.status_code, calls["n"]), (500, 1))

        seq = iter([httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={})])
        http, _ = self._http([(lambda r: "/sell/" in r.url.path, lambda r: next(seq))])
        resp = await http.request("negotiation", "POST", url, idempotent=False)
        self.assertEqual(resp.status_code, 200)

    async def test_401_twice_is_returned_not_looped(self):
        calls = {"n": 0}

        def always_401(req):
            calls["n"] += 1
            return httpx.Response(401)

        http, rec = self._http([(lambda r: "/sell/" in r.url.path, always_401)])
        resp = await http.request("fulfillment", "GET", "https://api.sandbox.ebay.com/sell/fulfillment/v1/order")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls["n"], 2)  # original call plus exactly one re-auth retry

    async def test_pacer_spaces_requests_at_2_per_second(self):
        from ebay_mcp.http import Pacer
        now, slept = [0.0], []

        async def fake_sleep(d):
            slept.append(d)
            now[0] += d

        pacer = Pacer(2.0, clock=lambda: now[0], sleep=fake_sleep)
        for _ in range(3):
            await pacer.wait()
        self.assertEqual(slept, [0.5, 0.5])

    async def test_daily_caps_are_per_api(self):
        http, _ = self._http([(lambda r: True, json_resp({}))])
        http.s.daily_limits = {"browse": 1, "fulfillment": 5}
        await http.request("browse", "GET", "https://api.sandbox.ebay.com/buy/browse/v1/x", auth="app")
        with self.assertRaises(RateLimitExceeded):
            await http.request("browse", "GET", "https://api.sandbox.ebay.com/buy/browse/v1/x", auth="app")
        resp = await http.request("fulfillment", "GET", "https://api.sandbox.ebay.com/sell/fulfillment/v1/order")
        self.assertEqual(resp.status_code, 200)

    async def test_daily_cap(self):
        http, _ = self._http([(lambda r: True, json_resp({}))])
        http.s.daily_limits = {"browse": 2}
        for _ in range(2):
            await http.request("browse", "GET", "https://api.sandbox.ebay.com/buy/browse/v1/x", auth="app")
        with self.assertRaises(RateLimitExceeded):
            await http.request("browse", "GET", "https://api.sandbox.ebay.com/buy/browse/v1/x", auth="app")

    async def test_iaf_header_for_trading(self):
        http, rec = self._http([(lambda r: r.url.path.endswith("api.dll"), lambda r: httpx.Response(200))])
        await http.request("trading", "POST", "https://api.sandbox.ebay.com/ws/api.dll", header_style="iaf")
        sent = rec.requests[-1]
        self.assertEqual(sent.headers["X-EBAY-API-IAF-TOKEN"], "v^1.1#access-123")
        self.assertNotIn("Authorization", sent.headers)

    def test_redact(self):
        self.assertEqual(redact("Bearer abc.def"), "Bearer ***")
        self.assertNotIn("secret", redact("token v^1.1#secretstuff here"))
        self.assertNotIn("dXNlcjpwYXNz", redact("Authorization: Basic dXNlcjpwYXNz"))
        self.assertNotIn("eyJhbGciOiJ", redact("jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln rejected"))


if __name__ == "__main__":
    unittest.main()
