"""Promotions are DATA, not code.

In every real commerce platform a promotion is a row a merchant configures, not
a branch an engineer deploys. Modelling it as data is not architectural taste --
it is the difference between "we can run that sale on Friday" and "that needs a
release". It also makes the whole rule space enumerable, which is what lets the
property tests generate promotions rather than hand-write them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Scope(str, Enum):
    ITEM = "item"          # applies to matching lines
    CATEGORY = "category"  # applies to lines in matching categories
    ORDER = "order"        # applies to the cart subtotal
    SHIPPING = "shipping"


class EffectKind(str, Enum):
    PERCENT_OFF = "percent_off"
    AMOUNT_OFF = "amount_off"
    BOGO = "bogo"                # buy N, get M cheapest free
    TIERED_SPEND = "tiered_spend"  # spend X, get Y off
    FREE_SHIPPING = "free_shipping"


class Stacking(str, Enum):
    EXCLUSIVE = "exclusive"    # if this one applies, nothing else does
    STACKABLE = "stackable"    # may combine with any other stackable
    # a promo may also declare a stack_class; two promos sharing a class are
    # mutually exclusive even when both are STACKABLE


@dataclass(frozen=True)
class Line:
    sku: str
    category: str
    unit_price_cents: int
    qty: int
    # Tax rate in basis points, PER LINE. Not a global: in the US the rate
    # depends on jurisdiction AND on what the item is -- groceries are commonly
    # exempt or reduced while the same cart's electronics are not. A single cart
    # rate is the assumption that makes discount allocation look easy and makes
    # the totals wrong.
    tax_bp: int = 0

    @property
    def subtotal(self) -> int:
        return self.unit_price_cents * self.qty


@dataclass(frozen=True)
class Cart:
    lines: tuple[Line, ...]
    shipping_cents: int = 0
    customer_segment: str = "regular"
    is_first_order: bool = False
    day_of_week: int = 0

    @property
    def subtotal(self) -> int:
        return sum(ln.subtotal for ln in self.lines)


@dataclass(frozen=True)
class Eligibility:
    categories: frozenset[str] = frozenset()      # empty = any
    skus: frozenset[str] = frozenset()            # empty = any
    min_subtotal_cents: int = 0
    min_qty: int = 0
    segments: frozenset[str] = frozenset()        # empty = any
    first_order_only: bool = False
    days_of_week: frozenset[int] = frozenset()    # empty = any
    # Scheduled activation/expiry as epoch seconds. None = unbounded. A promotion
    # that a merchant scheduled for Friday must not fire on Thursday, and the
    # engine -- not a cron job that flips a flag -- is where that is enforced.
    starts_at: float | None = None
    ends_at: float | None = None


@dataclass(frozen=True)
class Promotion:
    promo_id: str
    scope: Scope
    kind: EffectKind
    eligibility: Eligibility = field(default_factory=Eligibility)
    # percent in basis points (2000 = 20%); amount in cents; bogo as (buy, free)
    percent_bp: int = 0
    amount_cents: int = 0
    bogo: tuple[int, int] = (0, 0)
    tier_threshold_cents: int = 0
    stacking: Stacking = Stacking.STACKABLE
    stack_class: str = ""
    priority: int = 100        # lower number applies first
    max_redemptions: int | None = None
    per_customer_limit: int | None = None

    def __post_init__(self):
        if self.percent_bp < 0 or self.percent_bp > 10_000:
            raise ValueError("percent_bp must be 0..10000")
        if self.amount_cents < 0:
            raise ValueError("amount_cents must be >= 0")


@dataclass
class LineResult:
    line: Line
    discount_cents: int = 0

    @property
    def paid(self) -> int:
        return self.line.subtotal - self.discount_cents


@dataclass
class Evaluation:
    lines: list[LineResult]
    shipping_paid_cents: int
    trace: list[dict]
    applied: list[str]
    rejected: list[tuple[str, str]]
    tax_cents: int = 0
    tax_by_line: tuple = ()

    @property
    def line_discount_total(self) -> int:
        return sum(lr.discount_cents for lr in self.lines)

    @property
    def merchandise_paid(self) -> int:
        return sum(lr.paid for lr in self.lines)

    @property
    def total_paid(self) -> int:
        """Merchandise after discount, plus shipping, plus tax.

        TAX IS COMPUTED ON THE POST-DISCOUNT LINE AMOUNT. That ordering is not
        arbitrary: in the US a retailer discount reduces the taxable receipt
        (the customer never paid that money), whereas a MANUFACTURER coupon
        generally does not, because the retailer is reimbursed. This engine
        implements the retailer-discount case and does not model the
        manufacturer case at all -- stated here because the difference is real
        money and an engine that has not chosen is doing both by accident.
        """
        return self.merchandise_paid + self.shipping_paid_cents + self.tax_cents
