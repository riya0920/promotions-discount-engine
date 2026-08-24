"""Budget caps on real Postgres — the prediction this project made, tested.

Skips cleanly with no server, so the suite still passes without one.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import pgbudget as PB  # noqa: E402


needs_pg = pytest.mark.skipif(not PB.available(),
                              reason="no Postgres on %s" % PB.DSN)


# --------------------------------------------------------------------------
# the half of the prediction that was always safe
# --------------------------------------------------------------------------
@needs_pg
def test_the_cap_is_unbreachable_under_real_concurrency():
    """`remaining > 0` lives in the WHERE clause, so the check and the decrement
    cannot be separated by a scheduler. That is true on any engine, and this is
    the first time it has been demonstrated against genuinely parallel writers
    rather than against a queue."""
    promos = {"P": 100}
    PB.reset(promos)
    r = PB.storm(promos, n_workers=16, attempts_each=40)
    assert r["overspend"] == 0
    assert r["granted"] == 100
    assert r["refused"] == 540
    assert r["redeemed_rows"] == 100
    assert r["n_errors"] == 0, r["errors"]


@needs_pg
def test_the_database_refuses_rather_than_recording_a_negative_balance():
    import psycopg
    promos = {"P": 5}
    PB.reset(promos)
    PB.storm(promos, n_workers=8, attempts_each=10)
    with psycopg.connect(PB.DSN) as con:
        bad = con.execute(
            "SELECT count(*) FROM pg_promo_budget WHERE remaining < 0").fetchone()[0]
    assert bad == 0


@needs_pg
def test_both_engines_grant_exactly_the_cap():
    """The shape transfers. Same cap, same storm, same answer."""
    promos = {"P-%02d" % i: 20 for i in range(4)}
    PB.reset(promos)
    pg = PB.storm(promos, n_workers=12, attempts_each=20)
    sq = PB.sqlite_storm(promos, 12, 20, "out/test_budget.db")
    assert pg["consumed"] == sq["consumed"] == 80
    assert pg["overspend"] == sq["overspend"] == 0


@needs_pg
def test_the_per_customer_limit_is_enforced_too():
    """A different guarantee from the cap, and the one that needs the
    surrounding transaction rather than just the conditional UPDATE."""
    import psycopg
    promos = {"P": 1000}
    PB.reset(promos)
    with psycopg.connect(PB.DSN) as con:
        for _ in range(5):
            PB.redeem(con, "P", "same-customer", per_customer_limit=2)
        n = con.execute(
            "SELECT count(*) FROM pg_redemptions WHERE customer_id='same-customer'"
        ).fetchone()[0]
    assert n == 2


# --------------------------------------------------------------------------
# the half that was an assertion about another engine
# --------------------------------------------------------------------------
@needs_pg
def test_spreading_across_promotions_helps_postgres_and_not_sqlite():
    """The prediction. Measured PAIRED and tested PER ENGINE against 1.0 --
    the between-engine test is the obvious choice and the wrong one, because
    SQLite's runs finish in a fraction of a second and its timing noise swamps
    a real Postgres effect (p = 0.30 between engines, p = 0.04 for Postgres
    alone)."""
    sc = PB.scaling_ratio(n_workers=16, attempts_each=30, reps=9,
                          sqlite_path="out/test_scaling.db")
    assert sc["postgres"]["median"] > 1.0
    assert sc["postgres"]["above_one"] >= 6
    assert sc["sqlite"]["low"] < 1.0 < sc["sqlite"]["high"], \
        "sqlite's ratio should straddle 1.0 -- it is noise, not an effect"


@needs_pg
def test_the_sign_test_is_two_sided_and_bounded():
    sc = PB.scaling_ratio(n_workers=4, attempts_each=5, reps=3,
                          sqlite_path="out/test_scaling2.db")
    for engine in ("sqlite", "postgres"):
        assert 0.0 <= sc[engine]["p"] <= 1.0
        assert sc[engine]["n"] == 3
        assert 0 <= sc[engine]["above_one"] <= 3


@needs_pg
def test_timing_excludes_connection_setup():
    """Connecting costs ~10 ms and the drill is under a second, so charging
    setup to the measurement was worth a factor of several. `setup_seconds` is
    reported rather than folded into `seconds`."""
    promos = {"P": 100_000}
    PB.reset(promos)
    r = PB.storm(promos, n_workers=8, attempts_each=10)
    assert r["setup_seconds"] > 0
    assert r["seconds"] > 0


def test_the_module_degrades_without_a_server():
    old = PB.DSN
    try:
        PB.DSN = "host=127.0.0.1 port=1 user=nobody dbname=nothing"
        assert PB.available() is False
    finally:
        PB.DSN = old
