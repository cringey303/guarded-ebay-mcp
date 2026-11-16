"""Pricing guardrails. Pure functions: no network, no clock, no files.

Money is always Decimal, never float. Floors are rounded UP to the cent so a
floor can never sit below the true break-even. "today" is always a parameter,
so every rule can be tested with a fixed date.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------------
# Constants you can edit
# --------------------------------------------------------------------------
# Fee model (eBay US, most categories). eBay charges the final value fee on the
# "total amount of the sale": item price + buyer-paid shipping + sales tax.
FEE_RATE = Decimal("0.136")            # 13.6%
TAX_RATE = Decimal("0.07")             # assumed average sales tax on the buyer's total
PER_ORDER_FEE = Decimal("0.40")        # per order when the order total is over $10
SMALL_ORDER_FEE = Decimal("0.30")      # per order when the order total is $10 or less
SMALL_ORDER_MAX = Decimal("10.00")
# Athletic shoes: 8% and no per-order fee when the total is $150 or more.
SNEAKER_FEE_RATE = Decimal("0.08")
SNEAKER_MIN_TOTAL = Decimal("150.00")
SNEAKER_PER_ORDER_FEE = Decimal("0.00")
# eBay leaf categories "Men's Shoes > Athletic Shoes" and "Women's Shoes > Athletic Shoes".
ATHLETIC_SHOE_CATEGORY_IDS = frozenset({"15709", "95672"})

# Quick estimate shown next to the exact break-even: cost x 1.17 + $1.
QUICK_MULTIPLIER = Decimal("1.17")
QUICK_ADD = Decimal("1.00")

# Floor rules
PROFIT_PCT = Decimal("0.10")           # "profit floor" nets at least 10% over cost
AGED_DEFAULT_LOSS = Decimal("0.10")    # loss allowed on items flagged aged (over 12 months)
AGED_MAX_LOSS = Decimal("0.20")        # hard cap, even if a cost record asks for more
WINTER_KEYWORDS = ("beanie", "balaclava", "thermal")
WINTER_MONTHS = frozenset({9, 10, 11, 12})   # Sep-Dec: hold winter items for profit

# What to assume about shipping when computing a floor. With calculated shipping
# the buyer pays about what the label costs, so both default to 0. If you offer
# free shipping, set ASSUMED_LABEL_COST to your typical label price.
ASSUMED_BUYER_SHIPPING = Decimal("0.00")
ASSUMED_LABEL_COST = Decimal("0.00")

# Write checks
MAX_PRICE_MULTIPLE = Decimal("3")      # typo guard: block a new price over 3x the current one
MIN_OFFER_DISCOUNT_PCT = Decimal("5")  # eBay requires offers to be at least 5% off

# Package weight sanity limits in pounds, chosen by whole words in the title.
# If several groups match, the highest limit wins.
WEIGHT_LIMITS_LB = (
    (("hat", "cap", "beanie", "balaclava", "headband"), 2),
    (("tee", "shirt", "thermal", "top", "hoodie", "sweatshirt", "crewneck", "sweater", "jersey",
      "shorts", "pants", "sweatpants"), 4),
    (("jacket", "parka", "coat", "fleece", "puffer", "vest", "anorak", "windbreaker"), 7),
    (("sneaker", "shoe", "dunk", "jordan", "air force", "af1", "air max", "yeezy"), 8),
)
DEFAULT_WEIGHT_LIMIT_LB = 10           # titles that match no keyword
# --------------------------------------------------------------------------

CENT = Decimal("0.01")
ZERO = Decimal("0")
STANDARD = "standard"
ATHLETIC_SHOES = "athletic_shoes"


def to_decimal(x) -> Decimal:
    """Convert via str() so a float like 62.1 becomes Decimal('62.1'), not 62.0999..."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, bool) or x is None:
        raise ValueError(f"Not a money amount: {x!r}")
    d = Decimal(str(x).strip())
    if not d.is_finite():
        raise ValueError(f"Not a money amount: {x!r}")
    return d


def money(x) -> Decimal:
    """Round a display amount to cents (half up). Do not use for floors."""
    return to_decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_cent(x) -> Decimal:
    """Round UP to the cent. Used for every floor and break-even."""
    return to_decimal(x).quantize(CENT, rounding=ROUND_CEILING)


def floor_cent(x) -> Decimal:
    return to_decimal(x).quantize(CENT, rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class FeeModel:
    fee_rate: Decimal = FEE_RATE
    tax_rate: Decimal = TAX_RATE
    per_order_fee: Decimal = PER_ORDER_FEE
    small_order_fee: Decimal = SMALL_ORDER_FEE
    small_order_max: Decimal = SMALL_ORDER_MAX
    sneaker_fee_rate: Decimal = SNEAKER_FEE_RATE
    sneaker_min_total: Decimal = SNEAKER_MIN_TOTAL
    sneaker_per_order_fee: Decimal = SNEAKER_PER_ORDER_FEE


@dataclass(frozen=True)
class PricingPolicy:
    fees: FeeModel = field(default_factory=FeeModel)
    profit_pct: Decimal = PROFIT_PCT
    aged_default_loss: Decimal = AGED_DEFAULT_LOSS
    aged_max_loss: Decimal = AGED_MAX_LOSS
    winter_keywords: tuple = WINTER_KEYWORDS
    winter_months: frozenset = WINTER_MONTHS
    assumed_buyer_shipping: Decimal = ASSUMED_BUYER_SHIPPING
    assumed_label_cost: Decimal = ASSUMED_LABEL_COST
    max_price_multiple: Decimal = MAX_PRICE_MULTIPLE
    min_offer_discount_pct: Decimal = MIN_OFFER_DISCOUNT_PCT
    weight_limits_lb: tuple = WEIGHT_LIMITS_LB
    default_weight_limit_lb: int = DEFAULT_WEIGHT_LIMIT_LB


@dataclass(frozen=True)
class CostRecord:
    item_id: str
    cost: Decimal                 # sheet column I (total cost, shipping and tax included)
    acquired: date                # sheet column A
    title: str = ""               # sheet column B
    aged_ok: bool = False         # the "Aged" flag; a loss is allowed only with this
    max_loss_pct: Decimal | None = None   # costs.json only; capped at AGED_MAX_LOSS
    floor_override: Decimal | None = None  # can raise the floor, never lower it
    status: str = ""
    sheet_row: int | None = None


@dataclass
class FloorResult:
    cost: Decimal
    break_even: Decimal
    quick_estimate: Decimal
    floor: Decimal
    rule: str
    months_held: int
    target_net: Decimal           # the net the rule requires (cost, cost + profit, or cost - loss)
    category: str = STANDARD
    buyer_shipping: Decimal = ZERO
    label_cost: Decimal = ZERO
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"cost": str(money(self.cost)), "break_even": str(self.break_even),
                "quick_estimate": str(self.quick_estimate), "floor": str(self.floor), "rule": self.rule,
                "months_held": self.months_held, "fee_category": self.category, "notes": list(self.notes)}


# ------------------------------------------------------------------ fee model
def category_for(category_id: str | None = None, category_name: str | None = None) -> str:
    """Map an eBay category to a fee category. Unknown means STANDARD (the higher fee)."""
    if category_id and str(category_id).strip() in ATHLETIC_SHOE_CATEGORY_IDS:
        return ATHLETIC_SHOES
    if category_name and "athletic shoes" in category_name.lower():
        return ATHLETIC_SHOES
    return STANDARD


def order_total(price, buyer_shipping=ZERO, fm: FeeModel = FeeModel()) -> Decimal:
    """What eBay calls the total amount of the sale: (price + shipping) x (1 + tax)."""
    return (to_decimal(price) + to_decimal(buyer_shipping)) * (1 + fm.tax_rate)


def _tier(total: Decimal, category: str, fm: FeeModel) -> tuple[Decimal, Decimal]:
    """(rate, per-order fee) for an order total. The single source of truth for fee tiers."""
    if category == ATHLETIC_SHOES and total >= fm.sneaker_min_total:
        return fm.sneaker_fee_rate, fm.sneaker_per_order_fee
    if total <= fm.small_order_max:
        return fm.fee_rate, fm.small_order_fee
    return fm.fee_rate, fm.per_order_fee


def fees(price, buyer_shipping=ZERO, category: str = STANDARD, fm: FeeModel = FeeModel()) -> Decimal:
    """Exact (unrounded) eBay fees for one sale under the fee model."""
    total = order_total(price, buyer_shipping, fm)
    rate, fixed = _tier(total, category, fm)
    return rate * total + fixed


def net(price, buyer_shipping=ZERO, label_cost=ZERO, category: str = STANDARD,
        fm: FeeModel = FeeModel()) -> Decimal:
    """What you keep: price - fees + buyer shipping - label cost."""
    price, ship = to_decimal(price), to_decimal(buyer_shipping)
    return price - fees(price, ship, category, fm) + ship - to_decimal(label_cost)


def break_even(cost, buyer_shipping=ZERO, label_cost=ZERO, category: str = STANDARD,
               fm: FeeModel = FeeModel()) -> Decimal:
    """Lowest price (in whole cents) whose net is at least `cost`.

    Solved exactly from the fee model. Inside one fee tier the net is a straight
    line in price, so the answer in that tier is either the solution of
        price x (1 - rate x T) = cost + label + fixed - shipping x (1 - rate x T)
    rounded up to the cent, or the first cent of the tier. We compute that for
    every tier, keep the candidates that really land in their tier and really
    cover the cost, and return the lowest.
    """
    cost, ship, label = to_decimal(cost), to_decimal(buyer_shipping), to_decimal(label_cost)
    if cost < 0 or ship < 0 or label < 0:
        raise ValueError("cost, buyer_shipping and label_cost must not be negative")
    t = 1 + fm.tax_rate

    def tier_at(p: Decimal) -> tuple[Decimal, Decimal]:
        return _tier(order_total(p, ship, fm), category, fm)

    def first_cent_with_total(threshold: Decimal, inclusive: bool) -> Decimal:
        """Lowest cent price whose order total is >= threshold (or > threshold)."""
        def reached(p: Decimal) -> bool:
            total = order_total(p, ship, fm)
            return total >= threshold if inclusive else total > threshold
        p = max(ceil_cent(threshold / t - ship), CENT)
        while not reached(p):
            p += CENT
        while p - CENT >= CENT and reached(p - CENT):
            p -= CENT
        return p

    # (tier, first cent price of that tier)
    tiers = [((fm.fee_rate, fm.small_order_fee), CENT),
             ((fm.fee_rate, fm.per_order_fee), first_cent_with_total(fm.small_order_max, inclusive=False))]
    if category == ATHLETIC_SHOES:
        tiers.append(((fm.sneaker_fee_rate, fm.sneaker_per_order_fee),
                      first_cent_with_total(fm.sneaker_min_total, inclusive=True)))

    best: Decimal | None = None
    for (rate, fixed), start in tiers:
        keep = 1 - rate * t
        if keep <= 0:
            raise ValueError("Fee model takes 100% or more of the price")
        p = max(ceil_cent((cost + label + fixed - ship * keep) / keep), start)
        while net(p, ship, label, category, fm) < cost and tier_at(p) == (rate, fixed):
            p += CENT  # guards against division rounding; normally never runs
        if tier_at(p) != (rate, fixed) or net(p, ship, label, category, fm) < cost:
            continue
        if best is None or p < best:
            best = p
    if best is None:  # cannot happen: the top tier has no upper bound
        raise ValueError("No break-even price found")
    return best


def quick_break_even(cost) -> Decimal:
    """Rule of thumb: cost x 1.17 + $1. For comparison only; floors use break_even()."""
    return ceil_cent(to_decimal(cost) * QUICK_MULTIPLIER + QUICK_ADD)


# ------------------------------------------------------------------ floor rules
def months_held(acquired: date, today: date) -> int:
    """Full months only: Apr 23 to Oct 22 is 5 months, Apr 22 to Oct 22 is 6."""
    m = (today.year - acquired.year) * 12 + (today.month - acquired.month)
    if today.day < acquired.day:
        m -= 1
    return max(m, 0)


def is_winter(title: str, p: PricingPolicy = PricingPolicy()) -> bool:
    t = (title or "").lower()
    return any(k in t for k in p.winter_keywords)


def compute_floor(rec: CostRecord, listing_title: str, today: date, p: PricingPolicy = PricingPolicy(),
                  category: str = STANDARD, buyer_shipping=None, label_cost=None) -> FloorResult:
    cost = to_decimal(rec.cost)
    ship = p.assumed_buyer_shipping if buyer_shipping is None else to_decimal(buyer_shipping)
    label = p.assumed_label_cost if label_cost is None else to_decimal(label_cost)

    def price_for(target: Decimal) -> Decimal:
        return break_even(target, ship, label, category, p.fees)

    be = price_for(cost)
    profit_target = cost * (1 + p.profit_pct)
    age = months_held(rec.acquired, today)
    title = listing_title or rec.title
    pct = f"{(p.profit_pct * 100).normalize():f}"
    notes: list[str] = []

    if is_winter(title, p) and today.month in p.winter_months:
        target, rule = profit_target, f"winter item in season: net at least {pct}% over cost"
    elif age < 6:
        target, rule = profit_target, f"held under 6 months: net at least {pct}% over cost"
    elif age < 12:
        target, rule = cost, "held 6-12 months: break-even"
    elif rec.aged_ok:
        loss = p.aged_default_loss if rec.max_loss_pct is None else to_decimal(rec.max_loss_pct)
        loss = min(max(loss, ZERO), p.aged_max_loss)
        target = cost * (1 - loss)
        rule = f"held over 12 months and flagged aged: up to {(loss * 100).normalize():f}% loss allowed"
    else:
        target, rule = cost, "held over 12 months, not flagged aged: break-even"
        notes.append("Flag this item as aged in the sheet to allow a loss.")

    if rec.aged_ok and age < 12:
        notes.append("Aged flag ignored: item held under 12 months.")
    floor = price_for(target)
    if rec.floor_override is not None:
        override = ceil_cent(rec.floor_override)
        if override > floor:
            floor = override
            rule += " (raised by the per-item floor override)"
        else:
            notes.append("Per-item floor override ignored: it is below the rule floor and can only raise it.")
    return FloorResult(cost=cost, break_even=be, quick_estimate=quick_break_even(cost), floor=floor, rule=rule,
                       months_held=age, target_net=target, category=category, buyer_shipping=ship,
                       label_cost=label, notes=notes)


# ------------------------------------------------------------------ write checks
def _below_floor(price: Decimal, fr: FloorResult, p: PricingPolicy, what: str) -> list[str]:
    if price < fr.floor:
        return [f"{what} ${price} is below the floor ${fr.floor} ({fr.rule})."]
    # Fees step up by 10 cents when an order crosses $10, so a price just above
    # the floor can net less than the floor price does. Check the net directly.
    if net(price, fr.buyer_shipping, fr.label_cost, fr.category, p.fees) < fr.target_net:
        return [f"{what} ${price} nets less than the rule requires after fees ({fr.rule})."]
    return []


def check_price(new_price, current_price, fr: FloorResult, p: PricingPolicy = PricingPolicy()) -> list[str]:
    new = to_decimal(new_price)
    problems = []
    if new != new.quantize(CENT):
        return ["Price must be in whole cents."]
    if new <= 0:
        return ["Price must be positive."]
    problems += _below_floor(new, fr, p, "New price")
    if current_price is not None and new > money(current_price) * p.max_price_multiple:
        problems.append(f"${new} is more than {p.max_price_multiple}x the current ${money(current_price)}; "
                        "likely a typo.")
    return problems


def offer_price_from_discount(current_price, discount_pct) -> Decimal:
    """Round DOWN so the buyer gets at least the stated discount."""
    pct = to_decimal(discount_pct)
    if not 0 < pct < 100:
        raise ValueError("discount_pct must be between 0 and 100")
    return floor_cent(to_decimal(current_price) * (1 - pct / 100))


def check_offer(offer_price, current_price, fr: FloorResult, p: PricingPolicy = PricingPolicy()) -> list[str]:
    offer, cur = to_decimal(offer_price), money(current_price)
    if offer != offer.quantize(CENT):
        return ["Offer price must be in whole cents."]
    if offer <= 0:
        return ["Offer price must be positive."]
    problems = _below_floor(offer, fr, p, "Offer")
    if cur <= 0:
        problems.append("Listing has no current price.")
    else:
        discount = (cur - offer) / cur * 100
        if discount < p.min_offer_discount_pct:
            problems.append(f"Offer is only {discount:.1f}% off; eBay requires at least "
                            f"{p.min_offer_discount_pct}%.")
    return problems


def weight_limit_lb(title: str, p: PricingPolicy = PricingPolicy()) -> int:
    t = (title or "").lower()
    hits = [limit for words, limit in p.weight_limits_lb
            if any(re.search(rf"\b{re.escape(w)}s?\b", t) for w in words)]
    return max(hits) if hits else p.default_weight_limit_lb


def check_weight(title: str, weight_lb: int, weight_oz: int, allow_unusual: bool = False,
                 p: PricingPolicy = PricingPolicy()) -> list[str]:
    problems = []
    if weight_lb < 0 or not 0 <= weight_oz < 16:
        return ["weight_lb must be 0 or more and weight_oz must be 0-15."]
    total_oz = weight_lb * 16 + weight_oz
    if total_oz <= 0:
        return ["Weight must be more than 0."]
    if weight_lb > 150:
        return ["Weight is over 150 lb, the carrier maximum."]
    limit = weight_limit_lb(title, p)
    if total_oz > limit * 16 and not allow_unusual:
        problems.append(f"{weight_lb} lb {weight_oz} oz is over the {limit} lb sanity limit for this kind of item. "
                        "Pass allow_unusual_weight=true only if that is really the packed weight.")
    return problems


def estimate_sale(sale_price, cost, fr: FloorResult, p: PricingPolicy = PricingPolicy()) -> dict:
    """Preview of fees, net and profit for a hypothetical sale at sale_price."""
    f = fees(sale_price, fr.buyer_shipping, fr.category, p.fees)
    n = net(sale_price, fr.buyer_shipping, fr.label_cost, fr.category, p.fees)
    return {"est_fees": str(money(f)), "est_net": str(money(n)), "est_profit": str(money(n - to_decimal(cost)))}
