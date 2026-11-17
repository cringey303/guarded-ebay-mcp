"""Google Sheets over its REST API with a service account. No Google SDK needed.

The cost reader uses a key with the read-only scope. The sale logger uses a
separate key with the read-write scope. Google cannot limit a key to one tab,
so what the logger may write is enforced in sales_log.py.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import httpx2 as httpx
import jwt  # PyJWT

READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(Exception):
    pass


def a1(tab: str, cells: str) -> str:
    """'Tab name'!A1:B2, with quotes in the tab name escaped the way Sheets expects."""
    return "'" + tab.replace("'", "''") + "'!" + cells


class SheetsClient:
    def __init__(self, sheet_id: str, sa_file: str, scope: str = READ_SCOPE, transport=None, clock=time.time):
        if scope not in (READ_SCOPE, WRITE_SCOPE):
            raise SheetsError("Unknown Google Sheets scope")
        try:
            self.sa = json.loads(Path(sa_file).expanduser().read_text(encoding="utf-8-sig"))
            self.sa["client_email"], self.sa["private_key"], self.sa["token_uri"]
        except (OSError, ValueError, KeyError, TypeError):
            raise SheetsError("The service account file is missing or is not a service account key") from None
        if not str(self.sa["token_uri"]).startswith("https://"):
            raise SheetsError("Service account token_uri must be https")
        self.sheet_id, self.scope = sheet_id, scope
        self._client = httpx.AsyncClient(timeout=20, transport=transport)
        self._clock = clock
        self._token: tuple[str, float] | None = None

    async def _access_token(self) -> str:
        if self._token and self._token[1] - 60 > self._clock():
            return self._token[0]
        now = int(self._clock())
        assertion = jwt.encode(
            {"iss": self.sa["client_email"], "scope": self.scope, "aud": self.sa["token_uri"],
             "iat": now, "exp": now + 3600},
            self.sa["private_key"], algorithm="RS256",
        )
        try:
            r = await self._client.post(self.sa["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            if r.status_code != 200:
                raise SheetsError(f"Google rejected the service account (HTTP {r.status_code}).")
            body = r.json()
            self._token = (body["access_token"], now + int(body.get("expires_in", 3600)))
        except (httpx.TransportError, ValueError, KeyError):
            raise SheetsError("Could not get a Google access token.") from None
        return self._token[0]

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        token = await self._access_token()
        try:
            r = await self._client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.TransportError:
            raise SheetsError("Could not reach Google Sheets.") from None
        if r.status_code != 200:
            hint = (" Share the sheet with the service account email (Viewer to read, Editor to write)."
                    if r.status_code == 403 else "")
            raise SheetsError(f"Google Sheets returned HTTP {r.status_code}.{hint}")
        return r

    async def read(self, rng: str) -> list[list]:
        """Cell values as stored: numbers as numbers (money as Decimal), dates as serial numbers."""
        url = (f"{API}/{self.sheet_id}/values/{quote(rng, safe='')}"
               "?valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER")
        r = await self._request("GET", url)
        try:
            return json.loads(r.content, parse_float=Decimal).get("values", [])
        except ValueError:
            raise SheetsError("Google Sheets returned an unreadable response.") from None

    async def batch_update(self, cells: list[tuple[str, str]]) -> int:
        """Write single cells in one request. Values are entered as if typed (formulas work)."""
        if self.scope != WRITE_SCOPE:
            raise SheetsError("This Sheets client is read-only.")
        if not cells:
            return 0
        body = {"valueInputOption": "USER_ENTERED",
                "data": [{"range": rng, "values": [[value]]} for rng, value in cells]}
        r = await self._request("POST", f"{API}/{self.sheet_id}/values:batchUpdate", json=body)
        try:
            return int(r.json().get("totalUpdatedCells", 0))
        except (ValueError, TypeError):
            return 0
