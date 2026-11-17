"""Business logic: previews, guardrails, confirmation, audit, undo.

Every write follows the same path:
  preview  -> read the live listing, read cost, check every rule, store the exact change, return change_id
  confirm  -> kill switch, claim the change_id (single use), read the live listing AGAIN,
              check every rule AGAIN, skip anything that drifted, write, log
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from .config import Settings
from .http import redact
from .pricing import (PricingPolicy, category_for, check_offer, check_price, check_weight, compute_floor,
                      estimate_sale, money, offer_price_from_discount, to_decimal)
from .rest import RestClient, summarize_fees
from .store import Store
from .trading import TradingClient, validate_item_id, validate_package_type

MAX_BATCH = 20
PRICE_CHUNK = 4          # ReviseInventoryStatus takes at most 4 listings per call
MAX_DIMENSION_IN = 36
FIXED_PRICE_TYPES = (None, "FixedPriceItem", "StoresFixedPrice")
NO_COST = "No cost record for this item ID, so all writes for it are blocked. Add the item ID to the cost sheet."


class GuardrailError(Exception):
    pass


def _cents(x) -> Decimal:
    """Decimal from tool input. 62 becomes 62.00; 62.555 is left alone so the cent check rejects it."""
    d = to_decimal(x)
    return money(d) if d == money(d) else d


def _utc_today():
    return datetime.now(timezone.utc).date()


class ResaleService:
    def __init__(self, settings: Settings, trading: TradingClient, rest: RestClient, costs, store: Store,
                 policy: PricingPolicy | None = None, today=None):
        self.s, self.trading, self.rest, self.costs, self.store = settings, trading, rest, costs, store
        self.p = policy or PricingPolicy()
        self._today = today or _utc_today  # pricing functions never read the clock themselves

    # ------------------------------------------------------------------ reads
    async def _floor(self, item: dict):
        """(cost record, FloorResult) or (None, None). The cost comes from the cost source only."""
        rec = await self.costs.get(item["item_id"])
        if rec is None:
            return None, None
        category = category_for(item.get("category_id"), item.get("category"))
        return rec, compute_floor(rec, item.get("title", ""), self._today(), self.p, category=category)

    async def floor_for(self, item: dict) -> dict:
        try:
            rec, fr = await self._floor(item)
        except Exception as exc:  # cost source down: report it, never guess a floor
            return {"cost": None, "floor": None, "blocked": f"Cost lookup failed: {redact(str(exc))[:200]}"}
        if rec is None:
            return {"cost": None, "floor": None, "blocked": NO_COST}
        return {**fr.as_dict(), "sheet_row": rec.sheet_row}

    async def get_pricing_floor(self, item_id: str) -> dict:
        item = await self.trading.get_item(validate_item_id(item_id))
        return {"item_id": item["item_id"], "title": item["title"], "price": item["price"],
                **(await self.floor_for(item))}

    async def list_listings(self, include_package: bool = True, limit: int = 25, offset: int = 0) -> dict:
        everything = await self.trading.get_active_listings()
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        page = everything[offset:offset + limit]
        now = datetime.now(timezone.utc)

        async def enrich(item):
            if include_package:  # one GetItem per listing: weight, dimensions and category
                full = await self.trading.get_item(item["item_id"])
                item.update(package=full.get("package"), category=full.get("category"),
                            category_id=full.get("category_id"))
            if item.get("start_time"):
                started = datetime.fromisoformat(item["start_time"].replace("Z", "+00:00"))
                item["days_listed"] = (now - started).days
            item["pricing"] = await self.floor_for(item)
            return item

        rows = await asyncio.gather(*(enrich(i) for i in page))
        out = {"total_active": len(everything), "offset": offset, "returned": len(rows), "listings": rows}
        if offset + len(rows) < len(everything):
            out["next_offset"] = offset + len(rows)
        if not include_package:
            out["note"] = "Without package details the fee category is unknown, so floors use the standard 13.6% fee."
        return out

    async def get_orders(self, days: int = 30, limit: int = 50) -> list[dict]:
        return await self.rest.get_orders(days=max(1, min(int(days), 90)), limit=max(1, min(int(limit), 200)))

    async def get_order_fees(self, order_id: str) -> dict:
        order = await self.rest.get_order(order_id)
        fees = summarize_fees(await self.rest.get_order_transactions(order_id))
        cols = fees["fee_columns"]
        tax = Decimal(order["ebay_collected_tax"])
        sheet = {
            "K_sale_date": (order.get("created") or "")[:10],
            "L_platform": "eBay",
            "M_sold_price": str(money(Decimal(order["item_subtotal"]) + tax)),
            "N_transaction_fees": cols["fees"],
            "O_tax": str(money(tax + Decimal(cols["tax"]))),
            "P_shipping_paid": order["buyer_shipping_paid"],
            "Q_payment_processing": cols["processing"],
            "R_label_cost": fees["label_cost"],
        }
        notes = ["K is the order date in UTC.",
                 "M includes the tax eBay collected and O holds that tax, so S = M-N-Q-O+P-R nets it out.",
                 "Fees are sorted by eBay fee type: only fees that say tax go in O, payment or processing "
                 "fees go in Q, everything else goes in N."]
        if fees["label_cost"] is None:
            notes.append("No eBay label charge found for this order yet (or the label was not bought through eBay).")
        if "REFUND" in fees["transaction_types"]:
            notes.append("This order has a refund. N covers the original sale only; check Seller Hub for fee credits.")
        return {"order": order, "fees": fees, "sheet_values": sheet, "notes": notes}

    async def search_comps(self, query: str, limit: int = 20, new_only: bool = True) -> dict:
        items = await self.rest.browse_search(query, max(1, min(int(limit), 50)), new_only)
        prices = sorted(Decimal(i["price"]) + Decimal(i["shipping"] or 0) for i in items)
        stats = {}
        if prices:
            mid = len(prices) // 2
            median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
            stats = {"count": len(prices), "min_delivered": str(money(prices[0])),
                     "median_delivered": str(money(median)), "max_delivered": str(money(prices[-1]))}
        return {"note": "ASKING prices from active listings, not sold prices. Items often sell for less.",
                "price_type": "asking", "stats": stats, "items": items}

    def recent_changes(self, item_id: str | None = None, limit: int = 20) -> list[dict]:
        if item_id:
            item_id = validate_item_id(item_id)
        return self.store.list_audit(item_id, max(1, min(int(limit), 100)))

    # ------------------------------------------------------- shared rule checks
    async def _live(self, item_id: str) -> dict:
        """Read the listing from eBay right now. Never cached."""
        item = await self.trading.get_item(validate_item_id(item_id))
        if item.get("status") not in (None, "Active"):
            raise GuardrailError(f"Listing {item_id} is {item['status']}, not active.")
        if item.get("has_variations"):
            raise GuardrailError(f"Listing {item_id} has variations; this server only handles single-item listings.")
        if item.get("listing_type") not in FIXED_PRICE_TYPES:
            raise GuardrailError(f"Listing {item_id} is not a fixed-price listing.")
        return item

    async def _price_row(self, c: dict) -> tuple[dict, list[str]]:
        """Build one price row from live data and return (row, problems)."""
        item = await self._live(str(c["item_id"]))
        new = _cents(c["new_price"])
        rec, fr = await self._floor(item)
        if rec is None:
            raise GuardrailError(NO_COST)
        cur = money(item["price"])
        problems = check_price(new, cur, fr, self.p)
        if cur == new:
            problems.append("New price equals current price.")
        row = {"item_id": item["item_id"], "title": item["title"], "old_price": str(cur), "new_price": str(new),
               "reason": str(c.get("reason") or "")[:200], **fr.as_dict(),
               "estimate_if_sold": estimate_sale(new, rec.cost, fr, self.p)}
        return row, problems

    async def _price_rows(self, changes: list[dict]) -> tuple[list[dict], list[dict]]:
        ok, blocked = [], []
        for c in changes:
            try:
                row, problems = await self._price_row(c)
                (blocked if problems else ok).append({**row, "problems": problems} if problems else row)
            except (GuardrailError, ValueError, ArithmeticError) as exc:
                blocked.append({"item_id": str(c.get("item_id")), "problems": [str(exc)]})
        return ok, blocked

    async def _offer_row(self, item_id: str, offer_price=None, discount_pct=None,
                         message: str = "") -> tuple[dict, list[str]]:
        item = await self._live(item_id)
        cur = money(item["price"])
        if (offer_price is None) == (discount_pct is None):
            raise GuardrailError("Give exactly one of offer_price or discount_pct.")
        offer = _cents(offer_price) if offer_price is not None else offer_price_from_discount(cur, discount_pct)
        rec, fr = await self._floor(item)
        problems = [NO_COST] if rec is None else check_offer(offer, cur, fr, self.p)
        if item["item_id"] not in await self.rest.find_eligible_items():
            problems.append("eBay reports no interested buyers (watchers or cart abandoners) for this listing right now.")
        row = {"item_id": item["item_id"], "title": item["title"], "listing_price": str(cur),
               "offer_price": str(offer), "discount_pct": f"{(cur - offer) / cur * 100:.1f}" if cur else None,
               "watchers": item.get("watchers"), "message": str(message or "")[:2000],
               "duration": "2 days, no counteroffers"}
        if rec is not None:
            row.update(fr.as_dict(), estimate_if_sold=estimate_sale(offer, rec.cost, fr, self.p))
        return row, problems

    async def _package_row(self, item_id: str, new: dict, allow_unusual_weight: bool,
                           reason: str = "") -> tuple[dict, list[str]]:
        item = await self._live(item_id)
        problems = []
        if (await self.costs.get(item["item_id"])) is None:
            problems.append(NO_COST)
        problems += check_weight(item["title"], new["weight_lb"], new["weight_oz"], allow_unusual_weight, self.p)
        old = item.get("package") or {}
        merged = dict(new)
        for k in ("length_in", "width_in", "depth_in", "package_type"):
            if merged.get(k) is None:  # keep what the listing already has
                merged[k] = old.get(k)
            elif k != "package_type" and not 0 < merged[k] <= MAX_DIMENSION_IN:
                problems.append(f"{k} must be between 1 and {MAX_DIMENSION_IN} inches.")
        try:
            validate_package_type(merged["package_type"])
        except ValueError as exc:
            problems.append(str(exc))
        if not problems and all(old.get(k) == merged[k] for k in merged):
            problems.append("Package details already match; nothing to change.")
        row = {"item_id": item["item_id"], "title": item["title"], "old_package": item.get("package"),
               "new_package": merged, "allow_unusual_weight": bool(allow_unusual_weight),
               "reason": str(reason or "package fix")[:200]}
        return row, problems

    # --------------------------------------------------------------- previews
    async def preview_price_changes(self, changes: list[dict], _undoes: int | None = None) -> dict:
        if not changes:
            raise GuardrailError("No changes given.")
        if len(changes) > MAX_BATCH:
            raise GuardrailError(f"At most {MAX_BATCH} listings per preview.")
        if len({str(c["item_id"]).strip() for c in changes}) != len(changes):
            raise GuardrailError("Duplicate item IDs in one preview.")
        ok, blocked = await self._price_rows(changes)
        return self._finish_preview("price", {"items": ok, "undoes": _undoes}, ok, blocked)

    async def preview_offer(self, item_id: str, offer_price=None, discount_pct=None, message: str = "") -> dict:
        row, problems = await self._offer_row(item_id, offer_price, discount_pct, message)
        if problems:
            return self._finish_preview("offer", {}, [], [{**row, "problems": problems}])
        return self._finish_preview("offer", {"item": row}, [row], [])

    async def preview_package_update(self, item_id: str, weight_lb: int, weight_oz: int, length_in=None,
                                     width_in=None, depth_in=None, package_type=None,
                                     allow_unusual_weight: bool = False, reason: str = "",
                                     _undoes: int | None = None) -> dict:
        new = {"weight_lb": int(weight_lb), "weight_oz": int(weight_oz), "length_in": length_in,
               "width_in": width_in, "depth_in": depth_in, "package_type": package_type or None}
        row, problems = await self._package_row(item_id, new, allow_unusual_weight, reason)
        if problems:
            return self._finish_preview("package", {}, [], [{**row, "problems": problems}])
        return self._finish_preview("package", {"item": row, "undoes": _undoes}, [row], [])

    def _finish_preview(self, kind, payload, ok, blocked) -> dict:
        out = {"preview_only": True, "kind": kind, "will_change": ok, "blocked": blocked}
        if ok:
            change_id, expires = self.store.create_pending(kind, payload)
            out["change_id"] = change_id
            out["expires_at"] = datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")
            out["next_step"] = ("Nothing has changed on eBay. Show this preview to Lucas. Only after he explicitly "
                                f"says yes, call confirm_change with change_id={change_id}.")
        else:
            out["next_step"] = "Nothing to confirm; every requested change was blocked."
        if not self.s.writes_enabled:
            out["writes_enabled"] = False
            out["note"] = "Writes are disabled (EBAY_MCP_WRITES_ENABLED=false); confirm_change will refuse."
        return out

    # ---------------------------------------------------------------- writes
    async def confirm_change(self, change_id: str) -> dict:
        """The only code path that writes to eBay."""
        if not self.s.writes_enabled:
            raise GuardrailError("Writes are disabled. Set EBAY_MCP_WRITES_ENABLED=true to allow changes.")
        change = self.store.claim_pending(change_id)  # single use: a second call fails here
        try:
            kind = change["kind"]
            if kind == "price":
                result = await self._apply_prices(change_id, change["items"])
            elif kind == "offer":
                result = await self._apply_offer(change_id, change["item"])
            elif kind == "package":
                result = await self._apply_package(change_id, change["item"])
            else:
                raise GuardrailError(f"Unknown change kind {kind}")
            if change.get("undoes") and result.get("applied"):
                self.store.mark_undone(change["undoes"], result["applied"][0]["audit_id"])
            self.store.finish_pending(change_id, "applied" if result.get("applied") else "failed")
            return result
        except Exception:
            self.store.finish_pending(change_id, "failed")
            raise

    def _log(self, change_id, kind, row, old, new, status, detail="") -> int:
        return self.store.log(change_id=change_id, kind=kind, item_id=row["item_id"], field=kind, old=old, new=new,
                              reason=row.get("reason", ""), status=status, detail=redact(str(detail)))

    async def _apply_prices(self, change_id: str, items: list[dict]) -> dict:
        go, skipped = [], []
        for planned in items:
            try:
                row, problems = await self._price_row(planned)
            except (GuardrailError, ValueError, ArithmeticError) as exc:
                row, problems = planned, [str(exc)]
            else:
                if row["old_price"] != planned["old_price"]:
                    problems = [f"Price changed on eBay since the preview (was ${planned['old_price']}, "
                                f"now ${row['old_price']}); preview again."]
            if problems:
                skipped.append({"item_id": planned["item_id"], "problems": problems})
                self._log(change_id, "price", planned, planned["old_price"], planned["new_price"], "skipped",
                          "; ".join(problems))
            else:
                go.append(row)
        applied, failed = [], []
        for start in range(0, len(go), PRICE_CHUNK):
            chunk = go[start:start + PRICE_CHUNK]
            try:
                await self.trading.revise_prices([(r["item_id"], Decimal(r["new_price"])) for r in chunk])
            except Exception as exc:  # log, then keep going with the next chunk
                for r in chunk:
                    self._log(change_id, "price", r, r["old_price"], r["new_price"], "failed", exc)
                    failed.append({"item_id": r["item_id"], "error": redact(str(exc))})
                continue
            for r in chunk:
                aid = self._log(change_id, "price", r, r["old_price"], r["new_price"], "applied")
                applied.append({**r, "audit_id": aid})
        return {"applied": applied, "skipped": skipped, "failed": failed}

    async def _apply_offer(self, change_id: str, planned: dict) -> dict:
        try:
            row, problems = await self._offer_row(planned["item_id"], offer_price=planned["offer_price"],
                                                  message=planned["message"])
        except (GuardrailError, ValueError, ArithmeticError) as exc:
            row, problems = planned, [str(exc)]
        else:
            if row["listing_price"] != planned["listing_price"]:
                problems = ["Listing price changed on eBay since the preview; preview again."]
        row = {**row, "reason": planned["message"][:200]}
        if problems:
            self._log(change_id, "offer", row, planned["listing_price"], planned["offer_price"], "skipped",
                      "; ".join(problems))
            return {"applied": [], "skipped": [{"item_id": planned["item_id"], "problems": problems}], "failed": []}
        try:
            res = await self.rest.send_offer(row["item_id"], Decimal(row["offer_price"]), row["message"])
        except Exception as exc:
            self._log(change_id, "offer", row, row["listing_price"], row["offer_price"], "failed", exc)
            return {"applied": [], "skipped": [],
                    "failed": [{"item_id": row["item_id"], "error": redact(str(exc))}]}
        aid = self._log(change_id, "offer", row, row["listing_price"], row["offer_price"], "applied", res)
        return {"applied": [{**row, **res, "audit_id": aid}], "skipped": [], "failed": [],
                "note": "Offers cannot be recalled through the API; they expire after 2 days."}

    async def _apply_package(self, change_id: str, planned: dict) -> dict:
        n = planned["new_package"]
        try:
            row, problems = await self._package_row(planned["item_id"], n, planned["allow_unusual_weight"],
                                                    planned["reason"])
        except (GuardrailError, ValueError, ArithmeticError) as exc:
            row, problems = planned, [str(exc)]
        else:
            if row["old_package"] != planned["old_package"]:
                problems = ["Package details changed on eBay since the preview; preview again."]
        if problems:
            self._log(change_id, "package", planned, planned["old_package"], n, "skipped", "; ".join(problems))
            return {"applied": [], "skipped": [{"item_id": planned["item_id"], "problems": problems}], "failed": []}
        try:
            await self.trading.revise_package(row["item_id"], n["weight_lb"], n["weight_oz"], n["length_in"],
                                              n["width_in"], n["depth_in"], n["package_type"])
        except Exception as exc:
            self._log(change_id, "package", row, row["old_package"], n, "failed", exc)
            return {"applied": [], "skipped": [],
                    "failed": [{"item_id": row["item_id"], "error": redact(str(exc))}]}
        aid = self._log(change_id, "package", row, row["old_package"], n, "applied")
        return {"applied": [{**row, "audit_id": aid}], "skipped": [], "failed": []}

    # ------------------------------------------------------------------ undo
    async def preview_undo(self, audit_id: int) -> dict:
        """Undo is just another preview: it goes through the same rules and needs confirm_change."""
        entry = self.store.get_audit(int(audit_id))
        if entry is None or entry["status"] != "applied":
            raise GuardrailError("No applied change with that audit_id.")
        if entry["undone_by"]:
            raise GuardrailError("That change was already undone.")
        reason = f"undo audit #{int(audit_id)}"
        if entry["kind"] == "price":
            out = await self.preview_price_changes(
                [{"item_id": entry["item_id"], "new_price": entry["old_value"], "reason": reason}],
                _undoes=int(audit_id))
        elif entry["kind"] == "package":
            old = entry["old_value"] or {}
            if not old.get("weight_lb") and not old.get("weight_oz"):
                raise GuardrailError("The old package had no weight recorded; set it manually instead.")
            # The old weight may be the unusual one that was fixed. Lucas still sees it and must confirm.
            out = await self.preview_package_update(
                entry["item_id"], old["weight_lb"], old["weight_oz"], old.get("length_in"), old.get("width_in"),
                old.get("depth_in"), old.get("package_type"), allow_unusual_weight=True, reason=reason,
                _undoes=int(audit_id))
        else:
            raise GuardrailError("Only price and package changes can be undone. Sent offers expire after 2 days.")
        out["undoes_audit_id"] = int(audit_id)
        return out
