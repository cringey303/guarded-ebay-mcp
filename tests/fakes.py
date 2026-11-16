"""In-memory stand-ins for eBay so service and server tests run offline."""
from datetime import date
from decimal import Decimal

from ebay_mcp.pricing import CostRecord
from ebay_mcp.service import ResaleService
from ebay_mcp.store import Store

from helpers import make_settings

CAP = "123456789012"      # 6-12 months old, cost 50 -> floor 58.99
BEANIE = "223456789012"   # winter item, holds for profit in October
NO_COST = "323456789012"  # listed but missing from the sheet


class FakeTrading:
    def __init__(self):
        self.items = {
            CAP: {"item_id": CAP, "title": "Supreme Camp Cap Black", "price": "70.00", "watchers": 4,
                  "start_time": "2026-08-01T12:00:00.000Z", "status": "Active", "has_variations": False,
                  "listing_type": "FixedPriceItem", "category_id": "52365", "category": "Hats",
                  "package": {"weight_lb": 25, "weight_oz": 0, "length_in": 10, "width_in": 8,
                              "depth_in": 4, "package_type": "PackageThickEnvelope"}},
            BEANIE: {"item_id": BEANIE, "title": "Supreme Overdyed Beanie Red", "price": "80.00", "watchers": 2,
                     "start_time": "2026-09-01T12:00:00.000Z", "status": "Active", "has_variations": False,
                     "package": None},
            NO_COST: {"item_id": NO_COST, "title": "Nike SB Dunk Low", "price": "150.00", "watchers": 0,
                      "start_time": "2026-10-01T12:00:00.000Z", "status": "Active", "has_variations": False,
                      "package": None},
        }
        self.price_calls, self.package_calls = [], []
        self.fail_prices = self.fail_package = False

    async def get_active_listings(self, max_pages=5):
        return [{k: v for k, v in i.items() if k != "package"} for i in self.items.values()]

    async def get_item(self, item_id):
        if item_id not in self.items:
            raise ValueError("Item not found")
        return dict(self.items[item_id])

    async def revise_prices(self, updates):
        if self.fail_prices:
            raise RuntimeError("eBay said no")
        self.price_calls.append(updates)
        for i, p in updates:
            self.items[i]["price"] = f"{p:.2f}"
        return [{"item_id": i, "price": str(p)} for i, p in updates]

    async def revise_package(self, item_id, lb, oz, l=None, w=None, d=None, t=None):
        if self.fail_package:
            raise RuntimeError("eBay said no, token Bearer abc.def.ghi")
        self.package_calls.append((item_id, lb, oz))
        self.items[item_id]["package"] = {"weight_lb": lb, "weight_oz": oz, "length_in": l, "width_in": w,
                                          "depth_in": d, "package_type": t}
        return {"item_id": item_id, "warnings": []}


class FakeRest:
    def __init__(self):
        self.eligible = {CAP}
        self.offers = []
        self.fail_offer = False

    async def find_eligible_items(self):
        return set(self.eligible)

    async def send_offer(self, listing_id, price, message="", quantity=1):
        if self.fail_offer:
            raise RuntimeError("offer rejected")
        self.offers.append((listing_id, price))
        return {"offers_sent": 3, "offer_ids": ["o1", "o2", "o3"]}

    async def get_order(self, order_id):
        return {"order_id": order_id, "created": "2026-10-20T15:00:00.000Z", "item_subtotal": "70.00",
                "buyer_shipping_paid": "6.25", "ebay_collected_tax": "5.60", "items": []}

    async def get_order_transactions(self, order_id):
        return [
            {"transactionType": "SALE", "bookingEntry": "CREDIT", "amount": {"value": "71.23"},
             "totalFeeAmount": {"value": "10.97"},
             "orderLineItems": [{"marketplaceFees": [{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "10.57"}},
                                                     {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER",
                                                      "amount": {"value": "0.40"}}]}]},
            {"transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "amount": {"value": "5.49"}},
        ]

    async def get_orders(self, days=30, limit=50):
        return []

    async def browse_search(self, q, limit=20, new=True):
        return [{"title": "a", "price": "60.00", "shipping": "5.00"}, {"title": "b", "price": "75.00", "shipping": "0"},
                {"title": "c", "price": "90.00", "shipping": None}]


class FakeCosts:
    def __init__(self):
        self.recs = {
            CAP: CostRecord(item_id=CAP, cost=Decimal("50"), acquired=date(2026, 1, 10), sheet_row=12),
            BEANIE: CostRecord(item_id=BEANIE, cost=Decimal("40"), acquired=date(2025, 1, 5), aged_ok=True),
        }

    async def get(self, item_id):
        return self.recs.get(item_id)


def make_service(writes=True, clock=None):
    s = make_settings(EBAY_MCP_WRITES_ENABLED="true" if writes else "false")
    store = Store(clock=clock) if clock else Store()
    t, r, c = FakeTrading(), FakeRest(), FakeCosts()
    svc = ResaleService(s, t, r, c, store, today=lambda: date(2026, 10, 22))
    return svc, t, r, c, store
