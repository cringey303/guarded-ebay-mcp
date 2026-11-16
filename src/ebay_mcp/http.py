"""Shared HTTP layer: auth headers, per-second pacing, daily caps, retries."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

import httpx2 as httpx

from .auth import TokenManager
from .config import Settings
from .store import Store

log = logging.getLogger("ebay_mcp.http")
# Bearer/Basic header values, eBay tokens (they start with v^1.1#) and JWTs.
_TOKEN_RE = re.compile(r"((?:Bearer|Basic)\s+|v\^1\.1#)[^\s\"'<>&]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


def redact(text: str) -> str:
    """Strip anything that looks like a token from text bound for logs or tool output."""
    return _JWT_RE.sub("***", _TOKEN_RE.sub(r"\1***", str(text)))


class RateLimitExceeded(Exception):
    pass


class EbayApiError(Exception):
    def __init__(self, status: int, message: str, errors: list | None = None):
        super().__init__(f"eBay API error {status}: {message}")
        self.status = status
        self.errors = errors or []


class Pacer:
    """Spaces requests out so bursts from Claude never hammer eBay."""

    def __init__(self, per_second: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval = 1.0 / per_second if per_second > 0 else 0
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._clock, self._sleep = clock, sleep

    async def wait(self):
        async with self._lock:
            now = self._clock()
            if self._next > now:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class EbayHttp:
    MAX_RETRIES = 3

    def __init__(self, settings: Settings, tokens: TokenManager, store: Store,
                 transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep):
        self.s, self.tokens, self.store = settings, tokens, store
        self.client = httpx.AsyncClient(timeout=30, transport=transport,
                                        headers={"User-Agent": "guarded-ebay-mcp/1.0"})
        self.pacer = Pacer(settings.requests_per_second, sleep=sleep)
        self._sleep = sleep

    async def aclose(self):
        await self.client.aclose()

    async def request(self, api: str, method: str, url: str, *, auth: str = "user",
                      header_style: str = "bearer", headers: dict | None = None,
                      idempotent: bool = True, **kw) -> httpx.Response:
        """auth: 'user' | 'app'. header_style: 'bearer' (REST) | 'iaf' (Trading API).

        Every attempt counts against the daily cap, because eBay counts retries too.
        429 is always retried (eBay did not process the call). 5xx and network
        errors are retried only when `idempotent` is true, so a write that may
        have gone through is never sent twice. A 401 triggers one token refresh.
        """
        limit = self.s.daily_limits.get(api)
        reauthed = False
        for attempt in range(self.MAX_RETRIES + 1):
            count = self.store.incr_calls(api)
            if limit and count > limit:
                raise RateLimitExceeded(f"Daily cap for {api} reached ({limit}). Try again tomorrow or raise the cap.")
            await self.pacer.wait()
            token = await (self.tokens.user_token() if auth == "user" else self.tokens.app_token())
            h = dict(headers or {})
            if header_style == "iaf":
                h["X-EBAY-API-IAF-TOKEN"] = token
            else:
                h["Authorization"] = f"Bearer {token}"
            try:
                resp = await self.client.request(method, url, headers=h, **kw)
            except httpx.TransportError as exc:
                if attempt == self.MAX_RETRIES or not idempotent:
                    raise EbayApiError(0, f"network error: {type(exc).__name__}") from None
                await self._backoff(attempt, None)
                continue
            if resp.status_code == 401 and not reauthed:
                self.tokens.invalidate(auth)
                reauthed = True
                continue
            if resp.status_code == 429 or (resp.status_code >= 500 and idempotent):
                if attempt == self.MAX_RETRIES:
                    return resp
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp
        return resp  # pragma: no cover

    async def _backoff(self, attempt: int, retry_after: str | None):
        if retry_after and retry_after.isdigit():
            delay = min(int(retry_after), 60)
        else:
            delay = min(2 ** attempt + random.random(), 30)
        log.warning("eBay throttled or failed; retrying in %.1fs", delay)
        await self._sleep(delay)


def raise_for_rest(resp: httpx.Response):
    if resp.status_code < 400:
        return
    try:
        errs = resp.json().get("errors", [])
        msg = "; ".join(f"{e.get('errorId')}: {e.get('message')}" for e in errs) or resp.reason_phrase
    except (ValueError, AttributeError, TypeError):
        errs, msg = [], resp.reason_phrase
    raise EbayApiError(resp.status_code, redact(msg), errs)
