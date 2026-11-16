import json

import httpx2 as httpx

from ebay_mcp.config import Settings

ENV = {
    "EBAY_ENV": "sandbox",
    "EBAY_CLIENT_ID": "LucasRoo-resale-SBX-abc",
    "EBAY_CLIENT_SECRET": "SBX-supersecret",
    "EBAY_REFRESH_TOKEN": "v^1.1#i^1#refresh-secret",
    "EBAY_RU_NAME": "Lucas_Root-LucasRoo-resale-abcdef",
    "EBAY_MCP_WRITES_ENABLED": "true",
}


def make_settings(**overrides) -> Settings:
    env = dict(ENV)
    env.update(overrides)
    return Settings.from_env(env)


class Recorder:
    """httpx MockTransport handler that records requests and replays canned responses."""

    def __init__(self, routes):
        self.routes = routes  # list of (predicate, response_factory)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for pred, factory in self.routes:
            if pred(request):
                return factory(request)
        return httpx.Response(404, json={"errors": [{"errorId": 0, "message": "no route"}]})

    def transport(self):
        return httpx.MockTransport(self)


def token_ok(req):
    return httpx.Response(200, json={"access_token": "v^1.1#access-123", "expires_in": 7200})


def is_token(req):
    return req.url.path.endswith("/identity/v1/oauth2/token")


def json_resp(body, status=200):
    return lambda req: httpx.Response(status, content=json.dumps(body).encode(),
                                      headers={"content-type": "application/json"})
