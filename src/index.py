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

        for p in promos:
            el = p.eligibility
            self.min_subtotal[p.promo_id] = max(
                el.min_subtotal_cents,
                p.tier_threshold_cents if p.kind.value == "tiered_spend" else 0)
            self.min_qty[p.promo_id] = el.min_qty

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

    def candidates(self, cart: Cart) -> list[Promotion]:
        """The promotions this cart could conceivably qualify for."""
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
            out.append(self.promos[pid])
        return out

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
    return evaluate(cart, catalogue.candidates(cart), redemptions,
                    price_floor_cents, customer_id=customer_id,
                    per_customer=per_customer, now=now)
