"""Tests for the second tranche: eligibility index, atomic budgets, tax, scheduling.

The index tests are the important ones. An index is an optimisation, and an
optimisation that changes the answer is a bug -- so the property asserted is
EQUALITY with the unindexed path over generated carts, not "the index returns
something reasonable".
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import budget as BUD  # noqa: E402
from src.engine import evaluate  # noqa: E402
from src.index import CompiledCatalogue, evaluate_indexed  # noqa: E402
from src.model import (Cart, Eligibility, EffectKind, Line, Promotion,  # noqa: E402
                       Scope, Stacking)
from tests.test_properties import carts, promo_sets  # noqa: E402

SETTINGS = settings(max_examples=250, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


# --------------------------------------------------------------------------
# the eligibility index
# --------------------------------------------------------------------------
@given(carts(), promo_sets(max_size=5))
@SETTINGS
def test_index_never_changes_the_answer(cart, promos):
    """THE property for this feature. Any divergence is a correctness bug, and
    this caught a real one: ORDER-scoped promotions were being indexed by their
    (unused) category field, hiding them from carts that lacked that category."""
    compiled = CompiledCatalogue(promos)
    a = evaluate(cart, promos)
    b = evaluate_indexed(cart, compiled)
    assert a.total_paid == b.total_paid
    assert [lr.discount_cents for lr in a.lines] == [lr.discount_cents for lr in b.lines]
    assert a.applied == b.applied


@given(carts(), promo_sets(max_size=5))
@SETTINGS
def test_candidates_are_a_superset_of_what_actually_applies(cart, promos):
    """Pruning may only remove promotions that could not have applied."""
    compiled = CompiledCatalogue(promos)
    cand_ids = {p.promo_id for p in compiled.candidates(cart)}
    applied = set(evaluate(cart, promos).applied)
    assert applied <= cand_ids


def test_order_scoped_promos_are_always_candidates():
    """The bug, pinned. An order-level promo carrying a category restriction is
    still universal, because is_eligible does not consult categories for ORDER
    scope."""
    order_promo = Promotion(
        "ORDER-20", Scope.ORDER, EffectKind.PERCENT_OFF,
        eligibility=Eligibility(categories=frozenset({"grocery"})),
        percent_bp=2000)
    compiled = CompiledCatalogue([order_promo])
    cart = Cart(lines=(Line("X", "apparel", 5000, 1),))     # no grocery at all
    assert "ORDER-20" in {p.promo_id for p in compiled.candidates(cart)}
    assert evaluate_indexed(cart, compiled).total_paid == evaluate(cart, [order_promo]).total_paid


def test_index_prunes_a_sku_scoped_promo_the_cart_cannot_match():
    p = Promotion("SKU-ONLY", Scope.ITEM, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(skus=frozenset({"NOT-IN-CART"})),
                  percent_bp=5000)
    compiled = CompiledCatalogue([p])
    cart = Cart(lines=(Line("SOMETHING-ELSE", "apparel", 5000, 1),))
    assert compiled.candidates(cart) == []


def test_index_prunes_on_min_subtotal():
    p = Promotion("BIG-SPEND", Scope.ORDER, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(min_subtotal_cents=100_00),
                  percent_bp=1000)
    compiled = CompiledCatalogue([p])
    assert compiled.candidates(Cart(lines=(Line("A", "x", 500, 1),))) == []
    assert len(compiled.candidates(Cart(lines=(Line("A", "x", 200_00, 1),)))) == 1


# --------------------------------------------------------------------------
# atomic budget caps
# --------------------------------------------------------------------------
@pytest.fixture()
def budget_db():
    path = os.path.join(tempfile.mkdtemp(), "b.db")
    con = BUD.init(path, fresh=True)
    yield path, con
    con.close()


def test_cap_is_exact_under_concurrency_with_no_application_lock(budget_db):
    path, con = budget_db
    BUD.register(con, "P", 250)
    granted, lock = [], threading.Lock()
    barrier = threading.Barrier(12)

    def worker(w):
        c = BUD.connect(path)
        got = 0
        barrier.wait()
        for i in range(100):
            if BUD.claim(c, "P", "cust%d-%d" % (w, i)):
                got += 1
        c.close()
        with lock:
            granted.append(got)

    ts = [threading.Thread(target=worker, args=(w,)) for w in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(granted) == 250
    assert BUD.check_drift(con) == []
    assert con.execute("SELECT remaining FROM promo_budget").fetchone()[0] == 0


def test_per_customer_limit_holds_under_concurrency(budget_db):
    path, con = budget_db
    BUD.register(con, "ONE", 10_000)
    granted, lock = [], threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        c = BUD.connect(path)
        got = 0
        barrier.wait()
        for _ in range(40):
            if BUD.claim(c, "ONE", "same-person", per_customer_limit=2):
                got += 1
        c.close()
        with lock:
            granted.append(got)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(granted) == 2
    assert BUD.check_drift(con) == []


def test_release_returns_a_redemption_and_is_idempotent(budget_db):
    path, con = budget_db
    BUD.register(con, "P", 5)
    assert BUD.claim(con, "P", "c1")
    rid = con.execute("SELECT redemption_id FROM redemptions").fetchone()[0]
    before = con.execute("SELECT remaining FROM promo_budget").fetchone()[0]
    assert BUD.release(con, "P", rid) is True
    assert BUD.release(con, "P", rid) is False       # retry must not double-refund
    after = con.execute("SELECT remaining FROM promo_budget").fetchone()[0]
    assert after == before + 1
    assert BUD.check_drift(con) == []


def test_remaining_can_never_go_negative(budget_db):
    path, con = budget_db
    BUD.register(con, "P", 1)
    assert BUD.claim(con, "P", "a") is True
    for _ in range(10):
        assert BUD.claim(con, "P", "b") is False
    assert con.execute("SELECT remaining FROM promo_budget").fetchone()[0] == 0


# --------------------------------------------------------------------------
# tax
# --------------------------------------------------------------------------
def test_tax_is_computed_on_the_post_discount_amount():
    cart = Cart(lines=(Line("A", "x", 10_000, 1, tax_bp=1000),))
    half = Promotion("HALF", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=5000)
    ev = evaluate(cart, [half])
    assert ev.merchandise_paid == 5_000
    assert ev.tax_cents == 500          # 10% of 5000, not of 10000
    assert ev.total_paid == 5_500


def test_exempt_lines_are_not_taxed_even_when_discounted():
    cart = Cart(lines=(Line("TAXED", "x", 10_000, 1, tax_bp=1000),
                       Line("EXEMPT", "grocery", 10_000, 1, tax_bp=0)))
    p = Promotion("ORDER-20", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=2000)
    ev = evaluate(cart, [p])
    assert ev.tax_by_line[1] == 0
    assert ev.tax_by_line[0] > 0
    # a single cart-level rate would have taxed the exempt line too
    flat = (ev.merchandise_paid * 1000) // 10_000
    assert ev.tax_cents < flat


def test_tax_by_line_sums_to_tax_total():
    cart = Cart(lines=(Line("A", "x", 3333, 3, tax_bp=875),
                       Line("B", "y", 777, 2, tax_bp=600)))
    ev = evaluate(cart, [])
    assert sum(ev.tax_by_line) == ev.tax_cents


@given(carts(), promo_sets(max_size=3))
@SETTINGS
def test_total_paid_still_never_negative_with_tax(cart, promos):
    ev = evaluate(cart, promos)
    assert ev.total_paid >= 0
    assert ev.tax_cents >= 0


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------
def test_promotion_does_not_fire_before_its_window():
    cart = Cart(lines=(Line("A", "x", 10_000, 1),))
    p = Promotion("FRI", Scope.ORDER, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(starts_at=1000.0, ends_at=2000.0),
                  percent_bp=3000)
    assert evaluate(cart, [p], now=500.0).applied == []
    assert dict(evaluate(cart, [p], now=500.0).rejected)["FRI"] == "not_yet_active"
    assert evaluate(cart, [p], now=1500.0).applied == ["FRI"]
    assert dict(evaluate(cart, [p], now=2500.0).rejected)["FRI"] == "expired"


def test_no_now_means_no_time_filtering():
    """Callers that do not pass a clock get the old behaviour, so adding
    scheduling did not silently change every existing evaluation."""
    cart = Cart(lines=(Line("A", "x", 10_000, 1),))
    p = Promotion("FRI", Scope.ORDER, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(starts_at=1000.0, ends_at=2000.0),
                  percent_bp=3000)
    assert evaluate(cart, [p]).applied == ["FRI"]


# --------------------------------------------------------------------------
# per-customer limits in the engine
# --------------------------------------------------------------------------
def test_engine_enforces_per_customer_limit():
    cart = Cart(lines=(Line("A", "x", 10_000, 1),))
    p = Promotion("ONCE", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=2000,
                  per_customer_limit=1)
    used = {("ONCE", "alice"): 1}
    assert evaluate(cart, [p], customer_id="bob", per_customer=used).applied == ["ONCE"]
    ev = evaluate(cart, [p], customer_id="alice", per_customer=used)
    assert ev.applied == []
    assert dict(ev.rejected)["ONCE"] == "per_customer_limit_reached"
