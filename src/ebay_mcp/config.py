"""Settings loaded from environment variables or an env file outside the repo.

Nothing secret lives in source code. Secrets are wrapped in `Secret` so they
cannot leak through repr(), logs or tool output by accident.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ENDPOINTS = {
    "sandbox": {
        "api": "https://api.sandbox.ebay.com",
        "apiz": "https://apiz.sandbox.ebay.com",
        "auth": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "trading": "https://api.sandbox.ebay.com/ws/api.dll",
    },
    "production": {
        "api": "https://api.ebay.com",
        "apiz": "https://apiz.ebay.com",
        "auth": "https://auth.ebay.com/oauth2/authorize",
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
        "trading": "https://api.ebay.com/ws/api.dll",
    },
}

# Least privilege. api_scope covers the Trading API calls, sell.inventory covers
# the Negotiation API, fulfillment is read-only, sell.finances reads fees.
# No account, marketing or refund scopes. (Scope names are the same in sandbox.)
USER_SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
)
APP_SCOPES = ("https://api.ebay.com/oauth/api_scope",)

_COL_RE = re.compile(r"^[A-Za-z]{1,2}$")


class ConfigError(Exception):
    pass


class Secret:
    """Holds a secret string; prints as *** everywhere."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


def parse_env_text(text: str) -> dict:
    """Parse KEY=VALUE lines. Later lines win. Supports quotes and trailing # comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def load_env_file(path: str | os.PathLike, environ: dict | None = None) -> dict:
    """Load an env file into `environ` (default os.environ).

    Real environment variables win over the file. On Linux and macOS the file
    must not be readable by group or other (the same check ssh does for private
    keys). Windows has no POSIX mode bits; keep the file inside your user
    profile, which other accounts cannot read by default.
    """
    target = os.environ if environ is None else environ
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"Env file not found: {p}")
    if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{p} is readable by other users. Run: chmod 600 {p}")
    # utf-8-sig drops the byte order mark Windows Notepad can add.
    for key, value in parse_env_text(p.read_text(encoding="utf-8-sig")).items():
        if not target.get(key):
            target[key] = value
    return target


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(e: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(e.get(key) or default)
    except ValueError:
        raise ConfigError(f"{key} must be a whole number") from None


def _col(e: Mapping[str, str], key: str, default: str) -> str:
    v = (e.get(key) or default).strip().upper()
    if not _COL_RE.match(v):
        raise ConfigError(f"{key} must be a column letter like V or AA")
    return v


def _default_daily_limits() -> dict:
    # Conservative caps, all below eBay's default daily quotas.
    return {"trading": 4000, "fulfillment": 2000, "finances": 2000,
            "negotiation": 500, "browse": 2000}


@dataclass
class Settings:
    env: str
    client_id: str
    client_secret: Secret
    refresh_token: Secret
    ru_name: str = ""
    marketplace_id: str = "EBAY_US"
    site_id: int = 0
    trading_compat_level: int = 1489
    writes_enabled: bool = False
    db_path: str = "~/.ebay-mcp/state.db"
    # cost basis
    cost_source: str = "json"  # json | sheets
    cost_json_path: str = "~/.ebay-mcp/costs.json"
    sheet_id: str = ""
    sheet_tab: str = "Lucas Resell"
    sheet_date_col: str = "A"
    sheet_title_col: str = "B"
    sheet_cost_col: str = "I"
    sheet_status_col: str = "J"
    sheet_item_id_col: str = "AB"
    sheet_aged_col: str = "AC"
    sheet_floor_col: str = "AD"
    google_sa_file: str = ""
    # sale logger (writes sales into a tab; defaults to the TEST tab on purpose)
    log_sheet_id: str = ""
    log_tab: str = "Lucas Resell Test"
    log_statuses: frozenset = frozenset({"listed"})
    log_tax_in_price: bool = True
    log_timezone: str = "America/Los_Angeles"
    sheet_source_col: str = "Z"
    sheet_edited_col: str = "AA"
    sheet_order_col: str = "AE"
    google_sa_writer_file: str = ""
    # rate limits
    daily_limits: dict = field(default_factory=_default_daily_limits)
    requests_per_second: float = 2.0
    # hosting
    transport: str = "stdio"  # stdio | streamable-http
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str = ""
    oauth_issuer: str = ""
    jwks_url: str = ""
    allowed_subjects: frozenset = frozenset()
    required_scopes: frozenset = frozenset()

    def __post_init__(self):
        if self.env not in ENDPOINTS:
            raise ConfigError("EBAY_ENV must be 'sandbox' or 'production'")
        if self.transport not in ("stdio", "streamable-http"):
            raise ConfigError("EBAY_MCP_TRANSPORT must be stdio or streamable-http")
        if self.cost_source not in ("json", "sheets"):
            raise ConfigError("EBAY_MCP_COST_SOURCE must be json or sheets")

    @property
    def urls(self) -> dict:
        return ENDPOINTS[self.env]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *,
                 require_refresh_token: bool = True) -> "Settings":
        e = dict(os.environ if environ is None else environ)
        if e.get("EBAY_MCP_ENV_FILE"):
            load_env_file(e["EBAY_MCP_ENV_FILE"], e)
        required = ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"]
        if require_refresh_token:
            required.append("EBAY_REFRESH_TOKEN")
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise ConfigError("Missing required settings: " + ", ".join(missing))
        limits = _default_daily_limits()
        for api in limits:
            limits[api] = _int(e, f"EBAY_MCP_DAILY_LIMIT_{api.upper()}", limits[api])
        s = cls(
            env=(e.get("EBAY_ENV") or "sandbox").strip().lower(),
            client_id=e["EBAY_CLIENT_ID"],
            client_secret=Secret(e["EBAY_CLIENT_SECRET"]),
            refresh_token=Secret(e.get("EBAY_REFRESH_TOKEN", "")),
            ru_name=e.get("EBAY_RU_NAME", ""),
            marketplace_id=e.get("EBAY_MARKETPLACE_ID") or "EBAY_US",
            site_id=_int(e, "EBAY_SITE_ID", 0),
            trading_compat_level=_int(e, "EBAY_TRADING_COMPAT_LEVEL", 1489),
            writes_enabled=_bool(e.get("EBAY_MCP_WRITES_ENABLED"), False),
            db_path=e.get("EBAY_MCP_DB") or "~/.ebay-mcp/state.db",
            cost_source=(e.get("EBAY_MCP_COST_SOURCE") or "json").strip().lower(),
            cost_json_path=e.get("EBAY_MCP_COST_JSON") or "~/.ebay-mcp/costs.json",
            sheet_id=e.get("EBAY_MCP_SHEET_ID", ""),
            sheet_tab=e.get("EBAY_MCP_SHEET_TAB") or "Lucas Resell",
            sheet_date_col=_col(e, "EBAY_MCP_SHEET_DATE_COL", "A"),
            sheet_title_col=_col(e, "EBAY_MCP_SHEET_TITLE_COL", "B"),
            sheet_cost_col=_col(e, "EBAY_MCP_SHEET_COST_COL", "I"),
            sheet_status_col=_col(e, "EBAY_MCP_SHEET_STATUS_COL", "J"),
            sheet_item_id_col=_col(e, "EBAY_MCP_SHEET_ITEMID_COL", "AB"),
            sheet_aged_col=_col(e, "EBAY_MCP_SHEET_AGED_COL", "AC"),
            sheet_floor_col=_col(e, "EBAY_MCP_SHEET_FLOOR_COL", "AD"),
            google_sa_file=e.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
            log_sheet_id=(e.get("EBAY_MCP_LOG_SHEET_ID") or e.get("EBAY_MCP_SHEET_ID") or "").strip(),
            log_tab=e.get("EBAY_MCP_LOG_TAB") or "Lucas Resell Test",
            log_statuses=frozenset(x.strip().lower() for x in (e.get("EBAY_MCP_LOG_STATUSES") or "Listed").split(",")
                                   if x.strip()),
            log_tax_in_price=_bool(e.get("EBAY_MCP_LOG_TAX_IN_PRICE"), True),
            log_timezone=e.get("EBAY_MCP_LOG_TZ") or "America/Los_Angeles",
            sheet_source_col=_col(e, "EBAY_MCP_SHEET_SOURCE_COL", "Z"),
            sheet_edited_col=_col(e, "EBAY_MCP_SHEET_EDITED_COL", "AA"),
            sheet_order_col=_col(e, "EBAY_MCP_SHEET_ORDER_COL", "AE"),
            google_sa_writer_file=e.get("GOOGLE_SHEETS_WRITER_FILE", ""),
            daily_limits=limits,
            transport=(e.get("EBAY_MCP_TRANSPORT") or "stdio").strip().lower(),
            host=e.get("EBAY_MCP_HOST") or "127.0.0.1",
            port=_int(e, "PORT", _int(e, "EBAY_MCP_PORT", 8000)),
            public_url=e.get("EBAY_MCP_PUBLIC_URL", "").strip(),
            oauth_issuer=e.get("EBAY_MCP_OAUTH_ISSUER", "").strip(),
            jwks_url=e.get("EBAY_MCP_JWKS_URL", "").strip(),
            allowed_subjects=frozenset(x.strip() for x in e.get("EBAY_MCP_ALLOWED_SUBJECTS", "").split(",")
                                       if x.strip()),
            required_scopes=frozenset(e.get("EBAY_MCP_REQUIRED_SCOPES", "").split()),
        )
        if s.cost_source == "sheets" and not (s.sheet_id and s.google_sa_file):
            raise ConfigError("Sheets cost source needs EBAY_MCP_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE")
        for name, value in (("EBAY_MCP_SHEET_ID", s.sheet_id), ("EBAY_MCP_LOG_SHEET_ID", s.log_sheet_id)):
            if value and not re.match(r"^[A-Za-z0-9_-]{10,100}$", value):
                raise ConfigError(f"{name} does not look like a spreadsheet ID")
        if not s.log_statuses or "sold" in s.log_statuses:
            raise ConfigError("EBAY_MCP_LOG_STATUSES must list statuses to log sales on, and never Sold")
        return s
