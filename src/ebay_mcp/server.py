"""MCP server entry point.

Uses the official MCP Python SDK 2.x, where FastMCP is named MCPServer
(from mcp.server.mcpserver import MCPServer).

Safety lives in service.py and pricing.py, not here and not in the prompt.
This file only declares the tools, their annotations and the error mapping.
"""
from __future__ import annotations

import logging
import os
import sys
from functools import wraps
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .auth import AuthError
from .config import ConfigError, Settings
from .costs import CostSourceError
from .http import EbayApiError, RateLimitExceeded, redact
from .service import GuardrailError, ResaleService

SERVER_NAME = "guarded-ebay-mcp"

INSTRUCTIONS = """Tools for Lucas's eBay resale listings (Supreme and Nike).
Rules for using them:
- update_price, send_offer_to_watchers, update_package_weight and undo_change only PREVIEW. They never change eBay.
- confirm_change is the only tool that writes to eBay.
- After a preview, show Lucas the preview (old value, new value, floor, rule) and stop. Wait for his explicit yes
  in a later message before calling confirm_change. Never preview and confirm in the same turn.
- Floors come from the cost in his sheet. You cannot supply or override a cost. If a change is blocked, say why.
- search_comps returns asking prices of active listings, not sold prices. Say so when you quote them.
- get_order_fees returns sheet_values for columns K-R of the "Lucas Resell" tab.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
PREVIEW = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
LOCAL_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                             open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


class PriceChange(BaseModel):
    item_id: str = Field(description="eBay item ID (digits only)")
    # JSON has no decimal type, so the number arrives as a float and is turned
    # into a Decimal (through its string form) before any pricing math.
    new_price: float = Field(gt=0, le=100000, description="New Buy It Now price in USD")
    reason: str = Field(default="", max_length=200, description="Why, for the audit log")


def _safe(fn):
    """Turn expected failures into tool errors Claude can read. Tokens are redacted.

    Anything unexpected is left alone: the SDK reports it as a generic error
    and keeps the exception text on the server.
    """
    @wraps(fn)
    async def inner(*a, **kw):
        try:
            return await fn(*a, **kw)
        except (GuardrailError, LookupError, ValueError, ArithmeticError, RateLimitExceeded, ConfigError,
                AuthError, CostSourceError, EbayApiError) as exc:
            raise ToolError(redact(str(exc))) from None
    return inner


def build_server(svc: ResaleService, **server_kw) -> MCPServer:
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, **server_kw)

    @mcp.tool(annotations=READ)
    @_safe
    async def list_listings(include_package: bool = True, limit: int = 25, offset: int = 0) -> dict:
        """Active listings: item ID, title, price, watchers, days listed, package weight and dimensions,
        cost and floor. Returns up to `limit` (max 100) starting at `offset`; follow next_offset for more."""
        return await svc.list_listings(include_package=include_package, limit=limit, offset=offset)

    @mcp.tool(annotations=READ)
    @_safe
    async def get_pricing_floor(item_id: str) -> dict:
        """Cost, exact break-even, quick estimate, floor and the rule that set the floor, for one listing."""
        return await svc.get_pricing_floor(item_id)

    @mcp.tool(annotations=PREVIEW)
    @_safe
    async def update_price(changes: Annotated[list[PriceChange], Field(min_length=1, max_length=20)]) -> dict:
        """PREVIEW ONLY. Check 1-20 price changes against the floor rules. Changes nothing on eBay.
        Returns a change_id for confirm_change."""
        return await svc.preview_price_changes([c.model_dump() for c in changes])

    @mcp.tool(annotations=PREVIEW)
    @_safe
    async def send_offer_to_watchers(item_id: str, offer_price: float | None = None,
                                     discount_pct: float | None = None, message: str = "") -> dict:
        """PREVIEW ONLY. Check an offer to interested buyers (watchers, cart abandoners). Sends nothing.
        Give offer_price OR discount_pct. At least 5% off, lasts 2 days, no counteroffers."""
        return await svc.preview_offer(item_id, offer_price, discount_pct, message)

    @mcp.tool(annotations=PREVIEW)
    @_safe
    async def update_package_weight(item_id: str, weight_lb: Annotated[int, Field(ge=0, le=150)],
                                    weight_oz: Annotated[int, Field(ge=0, le=15)],
                                    length_in: int | None = None, width_in: int | None = None,
                                    depth_in: int | None = None, package_type: str | None = None,
                                    allow_unusual_weight: bool = False, reason: str = "") -> dict:
        """PREVIEW ONLY. Check a fix to a listing's packed weight, dimensions (whole inches) and package type.
        Changes nothing on eBay. Dimensions and package type you leave out keep their current values."""
        return await svc.preview_package_update(item_id, weight_lb, weight_oz, length_in, width_in,
                                                depth_in, package_type, allow_unusual_weight, reason)

    @mcp.tool(annotations=WRITE)
    @_safe
    async def confirm_change(change_id: str) -> dict:
        """WRITES TO EBAY. Applies one previewed change. Call only after Lucas has seen the preview and
        explicitly said yes. A change_id works once and expires 15 minutes after the preview."""
        return await svc.confirm_change(change_id)

    @mcp.tool(annotations=PREVIEW)
    @_safe
    async def undo_change(audit_id: int) -> dict:
        """PREVIEW ONLY. Preview reverting a logged price or package change. Apply it with confirm_change."""
        return await svc.preview_undo(audit_id)

    @mcp.tool(annotations=LOCAL_READ)
    @_safe
    async def recent_changes(item_id: str | None = None, limit: int = 20) -> list[dict]:
        """Audit log of every change attempted through this server, newest first: item ID, old value,
        new value, UTC time, reason, status (applied, failed, skipped) and change_id."""
        return svc.recent_changes(item_id, limit)

    @mcp.tool(annotations=READ)
    @_safe
    async def get_orders(days: int = 30, limit: int = 50) -> list[dict]:
        """Recent orders: order number, items, price, buyer-paid shipping, eBay-collected tax, ship-by date.
        Buyer names and addresses are never returned."""
        return await svc.get_orders(days, limit)

    @mcp.tool(annotations=READ)
    @_safe
    async def get_order_fees(order_id: str) -> dict:
        """Exact eBay transaction fees, fee breakdown and label cost for one order, plus values for sheet
        columns K-R (K sale date, L eBay, M sold price, N fees, O tax, P shipping paid, Q 0.00, R label)."""
        return await svc.get_order_fees(order_id)

    @mcp.tool(annotations=READ)
    @_safe
    async def search_comps(query: str, limit: int = 20, new_only: bool = True) -> dict:
        """Active eBay listings for a search with min, median and max delivered price.
        These are ASKING prices, not sold prices."""
        return await svc.search_comps(query, limit, new_only)

    return mcp


def build_service(settings: Settings) -> ResaleService:
    from .auth import TokenManager
    from .costs import JsonCostSource, SheetsCostSource
    from .http import EbayHttp
    from .rest import RestClient
    from .store import Store
    from .trading import TradingClient

    store = Store(settings.db_path)
    http = EbayHttp(settings, TokenManager(settings), store)
    if settings.cost_source == "sheets":
        costs = SheetsCostSource(
            settings.sheet_id, settings.sheet_tab, settings.google_sa_file,
            item_col=settings.sheet_item_id_col, aged_col=settings.sheet_aged_col,
            floor_col=settings.sheet_floor_col, date_col=settings.sheet_date_col,
            title_col=settings.sheet_title_col, cost_col=settings.sheet_cost_col,
            status_col=settings.sheet_status_col)
    else:
        costs = JsonCostSource(settings.cost_json_path)
    return ResaleService(settings, TradingClient(settings, http), RestClient(settings, http), costs, store)


def main():
    # stdout carries the MCP protocol over stdio, so logs go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("EBAY_MCP_LOG", "WARNING"))
    try:
        settings = Settings.from_env()
        if settings.transport == "stdio":
            build_server(build_service(settings)).run("stdio")
        else:
            from .remote import run_http
            run_http(settings, build_service, build_server)
    except (ConfigError, CostSourceError) as exc:
        sys.exit(f"{SERVER_NAME}: {exc}")


if __name__ == "__main__":
    main()
