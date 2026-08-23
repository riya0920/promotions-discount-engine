"""Compiled promotion catalogue with an eligibility index.

THE PROBLEM THIS SOLVES, restated from the benchmark that motivated it: naive
evaluation is O(promos x lines) because every promotion in the catalogue is
tested against every cart. Measured, that was 20 ms at p99 with 500 active
promotions against a 25 ms checkout budget -- fitting, but only just, and linear
in catalogue size so it stops fitting.

THE INDEX
---------
Most promotions are scoped to a category or a set of SKUs, so most promotions
cannot possibly apply to a given cart. The index answers "which promotions could
this cart even be eligible for?" without evaluating any of them:

    category -> promo ids     for CATEGORY-scoped promos
    sku      -> promo ids     for ITEM-scoped promos
    universal                 order/shipping-scoped, and item/category promos
                              with no sku or category restriction

A cart's candidate set is the union of the buckets its lines touch, plus the
universal set. Everything else is skipped before eligibility is ever called.

WHAT THE INDEX CANNOT PRUNE, and this is the honest limit: a promotion with no
category or SKU restriction is universal by definition, and universal promos are
common (order-level percentage discounts, free shipping, first-order offers). The
index makes the catalogue's SCOPED tail nearly free and does nothing for its
universal head, so the speedup is a function of catalogue composition rather
than catalogue size. The benchmark reports both.

MIN-SUBTOTAL PRUNING is the second lever and it is cheaper still: a promotion
requiring a $50 subtotal cannot apply to a $12 cart, and that is one comparison
against a precomputed threshold rather than a full eligibility call.
"""
from __future__ import annotations

from collections import defaultdict

from .model import Cart, Promotion, Scope


class CompiledCatalogue:
    """Built once when the merchant's promotion set changes, not per cart.

    That is the other half of the win: the compile cost is amortised over every
    checkout until a merchant edits a promotion, and checkout never pays it.
    """

    def __init__(self, promos: list[Promotion]):
        self.promos = {p.promo_id: p for p in promos}
        self.by_category: dict[str, set[str]] = defaultdict(set)
        self.by_sku: dict[str, set[str]] = defaultdict(set)
        self.universal: set[str] = set()
        # cheapest possible prune: the smallest subtotal that could ever qualify
        self.min_subtotal: dict[str, int] = {}
        self.min_qty: dict[str, int] = {}
        # THE PRUNES THE UNIVERSAL BUCKET NEEDED.
        #
        # The previous index pruned on product scope and spend thresholds only,
        # and its own report said so: "the index does not prune on segment,
        # first-order or day-of-week -- and those would help exactly the
        # universal bucket it currently cannot touch." That bucket is where the
        # broad-match catalogue's cost lives, so it is the only place a further
        # prune can pay. These three are all cart ATTRIBUTES rather than line
        # attributes, which is why they work on universal promotions where a
        # category index cannot.
        self.segments: dict[str, frozenset] = {}
        self.first_order_only: dict[str, bool] = {}
        self.days_of_week: dict[str, frozenset] = {}
        self.window: dict[str, tuple] = {}

        for p in promos:
            el = p.eligibility
            self.min_subtotal[p.promo_id] = max(
                el.min_subtotal_cents,
                p.tier_threshold_cents if p.kind.value == "tiered_spend" else 0)
            self.min_qty[p.promo_id] = el.min_qty
            self.segments[p.promo_id] = el.segments
            self.first_order_only[p.promo_id] = el.first_order_only
            self.days_of_week[p.promo_id] = el.days_of_week
            self.window[p.promo_id] = (el.starts_at, el.ends_at)

            # ORDER and SHIPPING scoped promotions are UNIVERSAL regardless of
            # what their eligibility carries, because is_eligible only consults
            # matching_lines() for ITEM and CATEGORY scope. Indexing them by
            # their (unused) category field was a real bug: it hid an
            # order-level promo from any cart that happened not to contain that
            # category, and the engine would have applied it. The equality
            # check against the unindexed path caught 39 mismatches on 300
            # carts, which is exactly what that check is for.
            if p.scope in (Scope.ORDER, Scope.SHIPPING):
                self.universal.add(p.promo_id)
            elif el.skus:
                for sku in el.skus:
                    self.by_sku[sku].add(p.promo_id)
            elif el.categories:
                for cat in el.categories:
                    self.by_category[cat].add(p.promo_id)
            else:
                # no product restriction -> could apply to any cart
                self.universal.add(p.promo_id)

        self.n_scoped = len(promos) - len(self.universal)

    def candidates(self, cart: Cart, now: float | None = None) -> list[Promotion]:
        """The promotions this cart could conceivably qualify for.

        Every check here must be a NECESSARY condition of eligibility, never a
        sufficient one: the index may only ever remove promotions the engine
        would have rejected anyway. Anything stronger changes the answer, which
        is not an optimisation. The equality property test against the unindexed
        path is what enforces that, and it has caught this exact class of bug
        before (39 wrong answers on 300 carts).
        """
        ids = set(self.universal)
        for ln in cart.lines:
            ids |= self.by_sku.get(ln.sku, frozenset())
            ids |= self.by_category.get(ln.category, frozenset())

        subtotal = cart.subtotal
        qty = sum(ln.qty for ln in cart.lines)
        out = []
        for pid in ids:
            if subtotal < self.min_subtotal[pid]:
                continue
            if qty < self.min_qty[pid]:
                continue
            segs = self.segments[pid]
            if segs and cart.customer_segment not in segs:
                continue
            if self.first_order_only[pid] and not cart.is_first_order:
                continue
            dows = self.days_of_week[pid]
            if dows and cart.day_of_week not in dows:
                continue
            if now is not None:
                starts, ends = self.window[pid]
                if starts is not None and now < starts:
                    continue
                if ends is not None and now > ends:
                    continue
            out.append(self.promos[pid])
        return out

    # ---------------------------------------------------------------- edits
    #
    # A merchant editing ONE promotion should not rebuild a catalogue of two
    # thousand. The previous version rebuilt wholesale and said so; the cost is
    # small here (6 ms at 2,000 promos) and it is the wrong shape -- a real
    # deployment edits continuously and rebuilding on every save is how a
    # promotions console becomes unusable at scale.
    #
    # These three methods keep the index consistent under single-promotion
    # changes. Consistency is asserted by a test that applies a random sequence
    # of edits and compares the incremental index against a freshly built one,
    # because an index that drifts from its source is worse than no index.
    def add(self, p: Promotion) -> None:
        if p.promo_id in self.promos:
            self.remove(p.promo_id)
        self.promos[p.promo_id] = p
        el = p.eligibility
        self.min_subtotal[p.promo_id] = max(
            el.min_subtotal_cents,
            p.tier_threshold_cents if p.kind.value == "tiered_spend" else 0)
        self.min_qty[p.promo_id] = el.min_qty
        self.segments[p.promo_id] = el.segments
        self.first_order_only[p.promo_id] = el.first_order_only
        self.days_of_week[p.promo_id] = el.days_of_week
        self.window[p.promo_id] = (el.starts_at, el.ends_at)
        if p.scope in (Scope.ORDER, Scope.SHIPPING):
            self.universal.add(p.promo_id)
        elif el.skus:
            for sku in el.skus:
                self.by_sku[sku].add(p.promo_id)
        elif el.categories:
            for cat in el.categories:
                self.by_category[cat].add(p.promo_id)
        else:
            self.universal.add(p.promo_id)
        self.n_scoped = len(self.promos) - len(self.universal)

    def remove(self, promo_id: str) -> None:
        if promo_id not in self.promos:
            return
        self.promos.pop(promo_id)
        self.universal.discard(promo_id)
        for bucket in (self.by_sku, self.by_category):
            for key in list(bucket):
                bucket[key].discard(promo_id)
                if not bucket[key]:
                    # Empty buckets are dropped rather than left behind. A
                    # long-lived index that never prunes them leaks one entry per
                    # SKU ever promoted, which is a slow memory leak that only
                    # shows up after months of merchandising.
                    del bucket[key]
        for d in (self.min_subtotal, self.min_qty, self.segments,
                  self.first_order_only, self.days_of_week, self.window):
            d.pop(promo_id, None)
        self.n_scoped = len(self.promos) - len(self.universal)

    def update(self, p: Promotion) -> None:
        self.add(p)

    def stats(self) -> dict:
        return dict(total=len(self.promos), universal=len(self.universal),
                    scoped=self.n_scoped,
                    categories_indexed=len(self.by_category),
                    skus_indexed=len(self.by_sku))


def evaluate_indexed(cart, catalogue: CompiledCatalogue, redemptions=None,
                     price_floor_cents: int = 0, customer_id: str | None = None,
                     per_customer=None, now: float | None = None):
    """Same engine, but only the candidate set is evaluated.

    The result must be IDENTICAL to evaluating the whole catalogue -- an index
    that changes the answer is not an optimisation, it is a bug. A property test
    asserts equality against the unindexed path on generated carts.
    """
    from .engine import evaluate
    return evaluate(cart, catalogue.candidates(cart, now=now), redemptions,
                    price_floor_cents, customer_id=customer_id,
                    per_customer=per_customer, now=now)
