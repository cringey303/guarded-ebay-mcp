"""eBay OAuth: user access tokens from a long-lived refresh token, plus an
application token for the Browse API.

Access tokens live only in memory (about 2 hours each). The refresh token
(about 18 months) comes from the environment and is never written anywhere
by this module. Error messages are built from eBay's error code only, so a
token or the client secret can never end up in one.
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from urllib.parse import urlencode

import httpx2 as httpx

from .config import APP_SCOPES, USER_SCOPES, Settings

REFRESH_MARGIN_S = 300  # refresh 5 minutes before expiry


class AuthError(Exception):
    pass


def _basic(settings: Settings) -> str:
    raw = f"{settings.client_id}:{settings.client_secret.get()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class TokenManager:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        self.s = settings
        self._clock = clock
        self._client = httpx.AsyncClient(timeout=20, transport=transport)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def aclose(self):
        await self._client.aclose()

    def invalidate(self, kind: str | None = None):
        if kind:
            self._cache.pop(kind, None)
        else:
            self._cache.clear()

    async def user_token(self) -> str:
        return await self._get("user")

    async def app_token(self) -> str:
        return await self._get("app")

    async def _get(self, kind: str) -> str:
        async with self._lock:  # one refresh at a time, no stampede
            hit = self._cache.get(kind)
            if hit and hit[1] - REFRESH_MARGIN_S > self._clock():
                return hit[0]
            if kind == "user":
                if not self.s.refresh_token:
                    raise AuthError("No refresh token. Run scripts/get_refresh_token.py first.")
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": self.s.refresh_token.get(),
                    "scope": " ".join(USER_SCOPES),
                }
            else:
                data = {"grant_type": "client_credentials", "scope": " ".join(APP_SCOPES)}
            try:
                resp = await self._client.post(
                    self.s.urls["token"],
                    data=data,
                    headers={"Authorization": _basic(self.s), "Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.TransportError as exc:
                raise AuthError(f"Could not reach eBay's token endpoint ({type(exc).__name__}).") from None
            if resp.status_code != 200:
                raise AuthError(_describe_token_error(resp))
            try:
                body = resp.json()
                token = body["access_token"]
            except (ValueError, KeyError, TypeError):
                raise AuthError("eBay's token endpoint returned an unexpected response.") from None
            self._cache[kind] = (token, self._clock() + int(body.get("expires_in", 7200)))
            return token


def _describe_token_error(resp: httpx.Response) -> str:
    try:
        err = str(resp.json().get("error", "unknown_error"))
    except (ValueError, AttributeError):
        err = "unparseable_response"
    if not re.fullmatch(r"[a-z_]{1,40}", err):  # only echo a plain OAuth error code
        err = "unknown_error"
    if err == "invalid_grant":
        return ("eBay rejected the refresh token (expired, revoked, or from the other environment). "
                "Run scripts/get_refresh_token.py again.")
    if err == "invalid_client":
        return "eBay rejected the client ID/secret. Check EBAY_CLIENT_ID and EBAY_CLIENT_SECRET for this EBAY_ENV."
    return f"Token request failed: HTTP {resp.status_code}, error={err}"


def new_state() -> str:
    return secrets.token_urlsafe(32)


def state_matches(expected: str, received: str) -> bool:
    """Constant-time comparison of the OAuth state value (CSRF check)."""
    return bool(expected) and secrets.compare_digest(expected.encode(), (received or "").encode())


def build_consent_url(settings: Settings, state: str | None = None) -> tuple[str, str]:
    """Return (url, state). Verify `state` with state_matches() when eBay redirects back."""
    if not settings.ru_name:
        raise AuthError("Set EBAY_RU_NAME (the RuName from your eBay keyset) first")
    state = state or new_state()
    q = {
        "client_id": settings.client_id,
        "redirect_uri": settings.ru_name,
        "response_type": "code",
        "scope": " ".join(USER_SCOPES),
        "state": state,
    }
    return f"{settings.urls['auth']}?{urlencode(q)}", state


async def exchange_code(settings: Settings, code: str, transport=None) -> dict:
    async with httpx.AsyncClient(timeout=20, transport=transport) as c:
        resp = await c.post(
            settings.urls["token"],
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.ru_name},
            headers={"Authorization": _basic(settings), "Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise AuthError(_describe_token_error(resp))
    body = resp.json()
    if not body.get("refresh_token"):
        raise AuthError("eBay returned no refresh token.")
    return body
