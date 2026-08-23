"""Tests for the completion pass: currency, linear resolution, the extended
index, incremental edits, BOGO policy, and the HTTP surface."""
from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import currency as CUR       # noqa: E402
from src import resolution as RES     # noqa: E402
from src.catalogue_store import CatalogueStore, from_json, to_json  # noqa: E402
from src.index import CompiledCatalogue, evaluate_indexed  # noqa: E402
from src.engine import evaluate       # noqa: E402
from src.model import (Cart, Eligibility, EffectKind, Line, Promotion, Scope,
                       Stacking)      # noqa: E402
from src.money import allocate        # noqa: E402


def _cart(subtotal=10000, segment="regular", first=False, dow=0, cats=None):
    cats = cats or ["apparel", "grocery", "electronics"]
    per = subtotal // len(cats)
    return Cart(tuple(Line("SKU%d" % i, c, per, 1) for i, c in enumerate(cats)),
                shipping_cents=799, customer_segment=segment,
                is_first_order=first, day_of_week=dow)


# --------------------------------------------------------------------------
# currency
# --------------------------------------------------------------------------
def test_a_currency_with_no_minor_unit_is_represented_correctly():
    """The gap the README called BREAKING: the yen IS the smallest unit."""
    assert CUR.minor_units_per_major("JPY") == 1
    assert CUR.format_minor(1234, "JPY") == "¥1234"


def test_a_three_decimal_currency_is_represented_correctly():
    assert CUR.minor_units_per_major("KWD") == 1000
    assert CUR.format_minor(1234, "KWD").endswith("1.234")


def test_an_unknown_currency_refuses_rather_than_defaulting():
    """Defaulting to USD would silently mis-scale every amount in the cart."""
    with pytest.raises(ValueError):
        CUR.get("XYZ")


@pytest.mark.parametrize("code", ["USD", "JPY", "KWD", "CHF"])
def test_the_allocator_still_sums_exactly_in_every_currency(code):
    sub = CUR.to_minor(1000.0, code)
    weights = [sub // 2, sub // 3, sub - sub // 2 - sub // 3]
    disc = sub // 5
    parts = allocate(disc, weights)
    assert sum(parts) == disc
    for p in parts:
        assert int(p) == p          # whole minor units only


def test_cash_rounding_is_separate_from_the_minor_unit():
    """Conflating them leaves an engine unable to represent a legal price."""
    assert CUR.get("CHF").exponent == 2
    assert CUR.apply_cash_rounding(1237, "CHF") == 1235
    assert CUR.apply_cash_rounding(1237, "USD") == 1237


def test_conversion_goes_through_major_units():
    """100 US cents at 150 JPY/USD is 150 yen, not 15,000."""
    assert CUR.convert(100, "USD", "JPY", 150.0) == 150


def test_conversion_requires_the_caller_to_supply_the_rate():
    import inspect
    sig = inspect.signature(CUR.convert)
    assert "rate_major_per_major" in sig.parameters
    assert sig.parameters["rate_major_per_major"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
def _promos():
    return [
        Promotion("A10", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=1000,
                  stack_class="order", priority=10),
        Promotion("A20", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=2000,
                  stack_class="order", priority=20),
        Promotion("C15", Scope.CATEGORY, EffectKind.PERCENT_OFF, percent_bp=1500,
                  eligibility=Eligibility(categories=frozenset({"apparel"})),
                  stack_class="cat", priority=30),
        Promotion("EX30", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=3000,
                  stacking=Stacking.EXCLUSIVE, priority=5),
    ]


def test_greedy_matches_brute_force_on_a_small_catalogue():
    v = RES.verify_against_bruteforce(_cart(), _promos())
    assert v["optimal"], v


def test_greedy_beats_the_zero_promo_baseline():
    cart = _cart()
    ev = RES.best_of_greedy(cart, _promos())
    none = evaluate(cart, [], None)
    assert ev.total_paid < none.total_paid


def test_brute_force_refuses_rather_than_truncating():
    """A cap returns the best of the first 12 with no signal that a better
    subset existed. That is a wrong answer that looks like a right one."""
    many = [Promotion("P%d" % i, Scope.ORDER, EffectKind.AMOUNT_OFF,
                      amount_cents=10 * (i + 1), stack_class="c%d" % i)
            for i in range(15)]
    with pytest.raises(ValueError):
        RES.best_of_bruteforce(_cart(), many, max_promos=12)


def test_greedy_handles_the_same_catalogue_the_brute_force_refuses():
    many = [Promotion("P%d" % i, Scope.ORDER, EffectKind.AMOUNT_OFF,
                      amount_cents=10 * (i + 1), stack_class="c%d" % i)
            for i in range(15)]
    ev = RES.best_of_greedy(_cart(), many)
    assert ev.total_paid > 0


def test_promos_without_a_stack_class_do_not_block_each_other():
    """Sharing one empty-string bucket was a bug: unrelated promotions became
    mutually exclusive because neither declared a class."""
    a = Promotion("A", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=100)
    b = Promotion("B", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=100)
    cls = RES._classes([a, b])
    assert len(cls) == 2


def test_an_exclusive_promo_is_evaluated_alone():
    cart = _cart(subtotal=1000)
    only_ex = [p for p in _promos() if p.stacking == Stacking.EXCLUSIVE]
    ev = RES.best_of_greedy(cart, only_ex)
    assert ev.applied == ["EX30"]


# --------------------------------------------------------------------------
# the extended index
# --------------------------------------------------------------------------
def _attr_promos():
    return [
        Promotion("VIP", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=1000,
                  eligibility=Eligibility(segments=frozenset({"vip"}))),
        Promotion("FIRST", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=500,
                  eligibility=Eligibility(first_order_only=True)),
        Promotion("TUES", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=300,
                  eligibility=Eligibility(days_of_week=frozenset({1}))),
        Promotion("ANY", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=100),
    ]


def test_segment_prunes_the_universal_bucket():
    idx = CompiledCatalogue(_attr_promos())
    ids = {p.promo_id for p in idx.candidates(_cart(segment="regular"))}
    assert "VIP" not in ids and "ANY" in ids


def test_first_order_prunes():
    idx = CompiledCatalogue(_attr_promos())
    ids = {p.promo_id for p in idx.candidates(_cart(first=False))}
    assert "FIRST" not in ids


def test_day_of_week_prunes():
    idx = CompiledCatalogue(_attr_promos())
    assert "TUES" not in {p.promo_id for p in idx.candidates(_cart(dow=0))}
    assert "TUES" in {p.promo_id for p in idx.candidates(_cart(dow=1))}


def test_the_schedule_window_prunes_only_when_a_clock_is_supplied():
    p = Promotion("LATER", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=100,
                  eligibility=Eligibility(starts_at=10_000.0))
    idx = CompiledCatalogue([p])
    assert "LATER" not in {q.promo_id for q in idx.candidates(_cart(), now=5_000.0)}
    assert "LATER" in {q.promo_id for q in idx.candidates(_cart(), now=20_000.0)}
    # No clock: no schedule prune. Guessing "now" inside an index is how a
    # simulator that asks "what would this cost on Friday" gets Thursday's answer.
    assert "LATER" in {q.promo_id for q in idx.candidates(_cart())}


def test_the_index_never_changes_the_answer():
    """The property that makes it an optimisation rather than a bug. This engine
    has been caught here before: 39 wrong answers on 300 carts."""
    rng = random.Random(4)
    promos = _attr_promos() + _promos()
    idx = CompiledCatalogue(promos)
    for _ in range(60):
        cart = _cart(subtotal=rng.choice([1000, 6000, 12000]),
                     segment=rng.choice(["regular", "vip"]),
                     first=rng.random() < 0.3, dow=rng.randrange(7))
        a = evaluate(cart, promos, None)
        b = evaluate_indexed(cart, idx)
        assert a.total_paid == b.total_paid, cart


# --------------------------------------------------------------------------
# incremental edits
# --------------------------------------------------------------------------
def _store(tmp_path):
    return CatalogueStore(str(tmp_path / "cat.db"), fresh=True)


def test_a_promotion_round_trips_through_json():
    p = _attr_promos()[0]
    assert from_json(to_json(p)) == p


def test_the_index_stays_consistent_under_random_edits(tmp_path):
    """An index that drifts from its source is worse than no index, because it
    is confidently wrong rather than absent."""
    st = _store(tmp_path)
    rng = random.Random(7)
    pool = _attr_promos() + _promos()
    for _ in range(120):
        if rng.random() < 0.3 and st.index.promos:
            st.delete(rng.choice(list(st.index.promos)))
        else:
            st.put(rng.choice(pool))
        assert st.index_matches_table()


def test_delete_removes_the_promotion_from_every_bucket(tmp_path):
    st = _store(tmp_path)
    p = Promotion("SKUP", Scope.ITEM, EffectKind.AMOUNT_OFF, amount_cents=100,
                  eligibility=Eligibility(skus=frozenset({"SKU0"})))
    st.put(p)
    assert "SKUP" in st.index.by_sku.get("SKU0", set())
    st.delete("SKUP")
    assert "SKUP" not in st.index.by_sku.get("SKU0", set())


def test_empty_buckets_are_dropped_rather_than_left_behind(tmp_path):
    """Otherwise the index leaks one entry per SKU ever promoted -- a slow leak
    that only appears after months of merchandising."""
    st = _store(tmp_path)
    p = Promotion("S", Scope.ITEM, EffectKind.AMOUNT_OFF, amount_cents=100,
                  eligibility=Eligibility(skus=frozenset({"GONE"})))
    st.put(p)
    st.delete("S")
    assert "GONE" not in st.index.by_sku


def test_every_edit_is_audited(tmp_path):
    """'Who turned this on at 4am' is otherwise unanswerable."""
    st = _store(tmp_path)
    p = _attr_promos()[0]
    st.put(p, actor="alice")
    st.put(p, actor="bob")
    st.delete(p.promo_id, actor="carol")
    h = st.history(p.promo_id)
    assert [x["action"] for x in h] == ["create", "update", "delete"]
    assert [x["actor"] for x in h] == ["alice", "bob", "carol"]


def test_deleting_something_that_does_not_exist_is_false_not_an_error(tmp_path):
    assert _store(tmp_path).delete("NOPE") is False


# --------------------------------------------------------------------------
# BOGO policy, pinned by example
# --------------------------------------------------------------------------
def _bogo_cart(prices):
    return Cart(tuple(Line("S%d" % i, "apparel", p, 1)
                      for i, p in enumerate(prices)))


def test_bogo_discounts_the_cheapest_in_the_group():
    """A POLICY, not a property. Properties cannot see policy -- BUGS_FOUND.md
    #5 -- so the only way to pin which unit is free is an example."""
    p = Promotion("B2G1", Scope.CATEGORY, EffectKind.BOGO, bogo=(2, 1),
                  eligibility=Eligibility(categories=frozenset({"apparel"})))
    ev = evaluate(_bogo_cart([1000, 2000, 3000]), [p], None)
    assert ev.line_discount_total == 1000


def test_bogo_needs_the_full_buy_quantity():
    p = Promotion("B3G1", Scope.CATEGORY, EffectKind.BOGO, bogo=(3, 1),
                  eligibility=Eligibility(categories=frozenset({"apparel"})))
    assert evaluate(_bogo_cart([1000, 2000]), [p], None).line_discount_total == 0


def test_bogo_repeats_for_each_complete_group():
    p = Promotion("B1G1", Scope.CATEGORY, EffectKind.BOGO, bogo=(1, 1),
                  eligibility=Eligibility(categories=frozenset({"apparel"})))
    ev = evaluate(_bogo_cart([1000, 1000, 1000, 1000]), [p], None)
    assert ev.line_discount_total == 2000


def test_bogo_never_discounts_more_than_the_cart_is_worth():
    p = Promotion("BIG", Scope.CATEGORY, EffectKind.BOGO, bogo=(1, 5),
                  eligibility=Eligibility(categories=frozenset({"apparel"})))
    cart = _bogo_cart([1000, 1000])
    ev = evaluate(cart, [p], None)
    assert ev.line_discount_total <= cart.subtotal


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------
def _client():
    tc = pytest.importorskip("fastapi.testclient")
    import serve
    return tc.TestClient(serve.app), serve


def test_price_returns_a_reason_for_every_promotion_that_did_not_apply():
    """A promo that vanishes silently is the most common promotions escalation
    there is."""
    client, serve = _client()
    body = client.post("/price", json={
        "lines": [{"sku": "T", "category": "apparel",
                   "unit_price_cents": 5000, "qty": 2}],
        "shipping_cents": 799}).json()
    ids = set(body["applied"]) | {x["promo_id"] for x in body["not_applied"]}
    assert ids == set(serve.STORE.index.promos)
    assert all(x["reason"] for x in body["not_applied"])


def test_a_merchant_edit_keeps_the_index_consistent():
    client, _ = _client()
    body = {"promo_id": "TEST1", "scope": "order", "kind": "amount_off",
            "percent_bp": 0, "amount_cents": 250, "bogo": [0, 0],
            "tier_threshold_cents": 0, "stacking": "stackable",
            "stack_class": "order", "priority": 50, "max_redemptions": None,
            "per_customer_limit": None,
            "eligibility": {"categories": [], "skus": [], "min_subtotal_cents": 0,
                            "min_qty": 0, "segments": [], "first_order_only": False,
                            "days_of_week": [], "starts_at": None, "ends_at": None}}
    r = client.put("/promotions/TEST1", json=body)
    assert r.status_code == 200 and r.json()["index_consistent"]
    d = client.delete("/promotions/TEST1")
    assert d.status_code == 200 and d.json()["index_consistent"]


def test_a_malformed_promotion_is_422_not_500():
    client, _ = _client()
    assert client.put("/promotions/BAD", json={"scope": "nonsense"}).status_code == 422


def test_deleting_an_unknown_promotion_is_404():
    client, _ = _client()
    assert client.delete("/promotions/NOSUCH").status_code == 404


def test_the_price_endpoint_can_simulate_a_future_date():
    """'What would this cart cost on Friday' is the question merchants ask, and
    a cron job flipping a flag cannot answer it."""
    client, _ = _client()
    future = client.post("/price", json={
        "lines": [{"sku": "T", "category": "apparel",
                   "unit_price_cents": 5000, "qty": 1}],
        "at": 4_000_000_000.0}).json()
    assert "total" in future
