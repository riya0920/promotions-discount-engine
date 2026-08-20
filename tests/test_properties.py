"""Property-based tests over generated carts x promotion sets.

Promotions look like if-statements and are actually a combinatorial correctness
problem. Stacking, allocation, ordering and rounding interact, and the
money-losing cases live in interactions that no example-based suite is going to
guess. So the carts and the promotions are both generated, and the assertions
are invariants rather than expected values.

Every violation these properties found during development is written up in
BUGS_FOUND.md with the minimal cart Hypothesis shrank it to.
"""
from __future__ import annotations

import os
import sys

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.engine import best_of_resolution, evaluate  # noqa: E402
from src.model import (Cart, Eligibility, EffectKind, Line, Promotion,  # noqa: E402
                       Scope, Stacking)
from src.money import allocate, pct_of  # noqa: E402

CATEGORIES = ["grocery", "apparel", "home", "electronics"]
SKUS = ["SKU%d" % i for i in range(8)]

SETTINGS = settings(max_examples=400, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


# --------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------
@st.composite
def lines(draw, min_size=1, max_size=5):
    n = draw(st.integers(min_size, max_size))
    out = []
    for _ in range(n):
        out.append(Line(sku=draw(st.sampled_from(SKUS)),
                        category=draw(st.sampled_from(CATEGORIES)),
                        unit_price_cents=draw(st.integers(1, 50_000)),
                        qty=draw(st.integers(1, 6))))
    return tuple(out)


@st.composite
def carts(draw, min_lines=1):
    return Cart(lines=draw(lines(min_size=min_lines)),
                shipping_cents=draw(st.integers(0, 2_000)),
                customer_segment=draw(st.sampled_from(["regular", "vip"])),
                is_first_order=draw(st.booleans()),
                day_of_week=draw(st.integers(0, 6)))


@st.composite
def eligibilities(draw, allow_thresholds=True):
    return Eligibility(
        categories=frozenset(draw(st.lists(st.sampled_from(CATEGORIES),
                                           max_size=2, unique=True))),
        skus=frozenset(draw(st.lists(st.sampled_from(SKUS), max_size=2, unique=True))),
        min_subtotal_cents=draw(st.integers(0, 30_000)) if allow_thresholds else 0,
        min_qty=draw(st.integers(0, 4)) if allow_thresholds else 0,
        segments=frozenset(draw(st.lists(st.sampled_from(["regular", "vip"]),
                                         max_size=1, unique=True))),
        first_order_only=draw(st.booleans()) if allow_thresholds else False,
    )


@st.composite
def promotions(draw, i=0, allow_thresholds=True, allow_exclusive=True,
               allow_stack_class=True, allow_bogo=True):
    kinds = list(EffectKind)
    if not allow_bogo:
        kinds = [k for k in kinds if k is not EffectKind.BOGO]
    kind = draw(st.sampled_from(kinds))
    scope = {EffectKind.FREE_SHIPPING: Scope.SHIPPING}.get(
        kind, draw(st.sampled_from([Scope.ITEM, Scope.CATEGORY, Scope.ORDER])))
    stacking = draw(st.sampled_from(list(Stacking))) if allow_exclusive \
        else Stacking.STACKABLE
    klass = draw(st.sampled_from(["", "", "seasonal", "loyalty"])) \
        if allow_stack_class else ""
    return Promotion(
        promo_id="P%02d" % i,
        scope=scope, kind=kind,
        eligibility=draw(eligibilities(allow_thresholds)),
        percent_bp=draw(st.integers(0, 10_000)),
        amount_cents=draw(st.integers(0, 20_000)),
        bogo=(draw(st.integers(1, 3)), draw(st.integers(1, 2))),
        tier_threshold_cents=draw(st.integers(0, 30_000)) if allow_thresholds else 0,
        stacking=stacking,
        stack_class=klass,
        priority=draw(st.integers(1, 200)),
    )


@st.composite
def promo_sets(draw, max_size=4, allow_thresholds=True, allow_exclusive=True,
               allow_stack_class=True, allow_bogo=True):
    n = draw(st.integers(0, max_size))
    return [draw(promotions(i, allow_thresholds, allow_exclusive,
                            allow_stack_class, allow_bogo))
            for i in range(n)]


# --------------------------------------------------------------------------
# money primitives
# --------------------------------------------------------------------------
@given(st.integers(0, 10 ** 9), st.lists(st.integers(0, 10 ** 6), min_size=1, max_size=12))
@SETTINGS
def test_allocation_is_exact(total, weights):
    parts = allocate(total, weights)
    assert sum(parts) == total
    assert len(parts) == len(weights)
    assert all(p >= 0 for p in parts) or total == 0


@given(st.integers(0, 10 ** 8), st.integers(0, 10_000))
@SETTINGS
def test_percentage_is_bounded_and_monotone(amount, bp):
    d = pct_of(amount, bp)
    assert 0 <= d <= amount
    assert pct_of(amount, min(bp + 1, 10_000)) >= d


def test_banker_rounding_on_ties():
    # 50 cents at 50% = 25 exactly, no tie
    assert pct_of(50, 5000) == 25
    # 5 cents at 50% = 2.5 -> ties to even -> 2
    assert pct_of(5, 5000) == 2
    # 15 cents at 50% = 7.5 -> ties to even -> 8
    assert pct_of(15, 5000) == 8


# --------------------------------------------------------------------------
# core invariants
# --------------------------------------------------------------------------
@given(carts(), promo_sets())
@SETTINGS
def test_total_is_never_negative(cart, promos):
    ev = evaluate(cart, promos)
    assert ev.total_paid >= 0
    assert ev.merchandise_paid >= 0
    assert ev.shipping_paid_cents >= 0


@given(carts(), promo_sets())
@SETTINGS
def test_no_line_is_discounted_below_zero(cart, promos):
    ev = evaluate(cart, promos)
    for lr in ev.lines:
        assert 0 <= lr.discount_cents <= lr.line.subtotal
        assert lr.paid >= 0


@given(carts(), promo_sets(), st.integers(0, 500))
@SETTINGS
def test_price_floor_is_respected(cart, promos, floor):
    ev = evaluate(cart, promos, price_floor_cents=floor)
    for lr in ev.lines:
        assert lr.paid >= min(floor * lr.line.qty, lr.line.subtotal)


@given(carts(), promo_sets())
@SETTINGS
def test_evaluation_is_deterministic(cart, promos):
    a = evaluate(cart, promos)
    b = evaluate(cart, list(reversed(promos)))
    assert a.total_paid == b.total_paid
    assert [lr.discount_cents for lr in a.lines] == [lr.discount_cents for lr in b.lines]
    assert a.applied == b.applied


@given(carts(), promo_sets())
@SETTINGS
def test_line_discounts_reconcile_to_the_cent(cart, promos):
    """Sum of per-line discounts equals the sum of every applied promo's
    line-level effect, exactly. This is the invariant that lets a partial refund
    be computed downstream."""
    ev = evaluate(cart, promos)
    from_trace = sum(sum(fr["line_discounts"]) for fr in ev.trace)
    assert ev.line_discount_total == from_trace
    assert ev.merchandise_paid == cart.subtotal - ev.line_discount_total


# --------------------------------------------------------------------------
# the interaction properties -- where the bugs actually were
# --------------------------------------------------------------------------
@given(carts(), promo_sets(max_size=3, allow_exclusive=False, allow_stack_class=False))
@SETTINGS
def test_adding_a_promotion_never_increases_the_total(cart, promos):
    """Monotonicity in the promotion catalogue.

    Restricted to promos that are genuinely combinable: STACKABLE and carrying no
    stacking class. Both restrictions were forced by counterexamples rather than
    chosen -- under priority-greedy resolution ANY mutual-exclusion mechanism
    breaks this property, because the winner is picked by scope and priority
    rather than by value. See BUGS_FOUND.md #1, and the pinned regression below.
    """
    assume(promos)
    base = evaluate(cart, promos[:-1]).total_paid
    more = evaluate(cart, promos).total_paid
    assert more <= base


@given(carts(), promo_sets(max_size=3))
@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_best_of_resolution_is_monotone_even_with_exclusives(cart, promos):
    """The same property that fails under priority-greedy holds under
    best-for-customer resolution. This is the demonstration that the violation is
    a policy choice, not a defect in the arithmetic."""
    assume(promos)
    base = best_of_resolution(cart, promos[:-1]).total_paid
    more = best_of_resolution(cart, promos).total_paid
    assert more <= base


@given(carts(min_lines=2),
       promo_sets(max_size=3, allow_thresholds=False, allow_bogo=False))
@SETTINGS
def test_removing_an_item_never_raises_the_price_of_what_remains(cart, promos):
    """The sneaky one.

    Both exclusions in the generator -- no thresholds, no BOGO -- are FINDINGS
    rather than conveniences. Any promotion whose qualification depends on the
    OTHER items in the cart necessarily breaks this property: removing a line can
    dissolve the qualification and raise what the survivors pay. That is correct
    retail behaviour, not a bug, and a merchant would not want it otherwise.
    What the property is really asserting, once those two classes are removed, is
    that nothing ELSE in the engine has that dependency by accident.
    See BUGS_FOUND.md #2 and #3.
    """
    full = evaluate(cart, promos)
    for drop in range(len(cart.lines)):
        kept = tuple(ln for i, ln in enumerate(cart.lines) if i != drop)
        smaller = Cart(lines=kept, shipping_cents=cart.shipping_cents,
                       customer_segment=cart.customer_segment,
                       is_first_order=cart.is_first_order,
                       day_of_week=cart.day_of_week)
        after = evaluate(smaller, promos)
        paid_before = sum(lr.paid for i, lr in enumerate(full.lines) if i != drop)
        assert after.merchandise_paid <= paid_before


@given(carts(), promo_sets(max_size=3))
@SETTINGS
def test_every_promotion_is_either_applied_or_has_a_reason(cart, promos):
    """Explainability as an invariant: a promo that silently vanishes is the
    single most common support escalation a promotions team gets."""
    ev = evaluate(cart, promos)
    accounted = set(ev.applied) | {pid for pid, _ in ev.rejected}
    assert accounted == {p.promo_id for p in promos}
    assert all(reason for _, reason in ev.rejected)


@given(carts(), promo_sets(max_size=3))
@SETTINGS
def test_trace_totals_match_the_evaluation(cart, promos):
    ev = evaluate(cart, promos)
    traced = sum(fr["total"] for fr in ev.trace)
    actual = (cart.subtotal - ev.merchandise_paid) + \
             (cart.shipping_cents - ev.shipping_paid_cents)
    assert traced == actual


# --------------------------------------------------------------------------
# pinned regressions: the two counterexamples Hypothesis shrank to
# --------------------------------------------------------------------------
def test_regression_stack_class_crowd_out():
    """BUGS_FOUND.md #1, minimised.

    A promotion worth NOTHING (0% off) sorts ahead on scope, claims the shared
    stacking class, and locks out a promotion that was worth something. Adding a
    promo to the catalogue made the cart more expensive.
    """
    cart = Cart(lines=(Line("SKU0", "grocery", 417, 6),))
    worthless = Promotion("P_ITEM_0PCT", Scope.ITEM, EffectKind.PERCENT_OFF,
                          percent_bp=0, stack_class="seasonal", priority=1)
    valuable = Promotion("P_CAT_20PCT", Scope.CATEGORY, EffectKind.PERCENT_OFF,
                         percent_bp=2000, stack_class="seasonal", priority=1)

    alone = evaluate(cart, [valuable])
    both = evaluate(cart, [worthless, valuable])

    assert alone.total_paid < both.total_paid          # the violation, pinned
    assert "P_CAT_20PCT" in dict(both.rejected)
    assert dict(both.rejected)["P_CAT_20PCT"] == "stack_class_conflict:seasonal"

    # and the documented fix recovers monotonicity
    fixed = best_of_resolution(cart, [worthless, valuable])
    assert fixed.total_paid == alone.total_paid


def test_regression_bogo_dissolves_when_a_line_is_removed():
    """BUGS_FOUND.md #2, minimised.

    Two 1-cent units with buy-1-get-1: one unit is free. Delete the PAID unit and
    the survivor no longer qualifies, so what it pays goes from 0 to 1. Correct
    BOGO semantics, and proof that the monotonicity property cannot be stated
    over cart-dependent promotions.
    """
    l0 = Line("SKU0", "grocery", 1, 1)
    l1 = Line("SKU0", "grocery", 1, 1)
    bogo = Promotion("P_BOGO", Scope.ITEM, EffectKind.BOGO, bogo=(1, 1))

    full = evaluate(Cart(lines=(l0, l1)), [bogo])
    assert full.merchandise_paid == 1                  # one of the two is free
    free_idx = 0 if full.lines[0].discount_cents else 1

    survivor = evaluate(Cart(lines=(full.lines[free_idx].line,)), [bogo])
    assert full.lines[free_idx].paid == 0
    assert survivor.merchandise_paid == 1              # the survivor now pays
