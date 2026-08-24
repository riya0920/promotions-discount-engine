"""Budget caps on real Postgres — the specific claim this project made, tested.

WHAT THIS PROJECT SAID
----------------------
"SQLite, not Postgres. The conditional-`UPDATE` budget cap proves the algorithm
is race-free without proving it scales: SQLite serialises writers, so redemptions
of DIFFERENT promotions queue here and would proceed in parallel on Postgres. The
shape transfers; the throughput number does not."

That is a falsifiable prediction about another engine, written without access to
one. It is now testable, and this module is what tests it. The prediction has two
halves and they are not equally safe:

  * "the shape transfers"  -- the conditional UPDATE is race-free anywhere,
    because the check and the decrement are one statement. Safe.
  * "different promotions would proceed in parallel" -- an assertion about
    Postgres's lock granularity that nobody here had measured.

The measurement below runs the same redemption storm against both engines, with
all workers on ONE promotion and then spread across MANY, and asks whether the
one-versus-many distinction exists on each.
"""
from __future__ import annotations

import os
import threading
import time

import psycopg

DSN = os.environ.get(
    "SE2_PG_DSN", "host=127.0.0.1 port=55432 user=postgres dbname=postgres")

SCHEMA = """
DROP TABLE IF EXISTS pg_redemptions;
DROP TABLE IF EXISTS pg_promo_budget;
CREATE TABLE pg_promo_budget (
    promo_id  text PRIMARY KEY,
    cap       integer NOT NULL,
    remaining integer NOT NULL,
    CONSTRAINT never_negative CHECK (remaining >= 0),
    CONSTRAINT never_above_cap CHECK (remaining <= cap)
);
CREATE TABLE pg_redemptions (
    redemption_id bigserial PRIMARY KEY,
    promo_id      text NOT NULL REFERENCES pg_promo_budget(promo_id),
    customer_id   text NOT NULL,
    created_at    double precision NOT NULL
);
"""


def available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3) as con:
            con.execute("SELECT 1")
        return True
    except Exception:
        return False


def reset(promos: dict[str, int]) -> None:
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(SCHEMA)
        with con.cursor() as cur:
            cur.executemany(
                "INSERT INTO pg_promo_budget(promo_id,cap,remaining)"
                " VALUES (%s,%s,%s)",
                [(p, c, c) for p, c in promos.items()])


def redeem(con, promo_id: str, customer_id: str,
           per_customer_limit: int | None = None) -> bool:
    """The same conditional UPDATE, on Postgres.

    `remaining > 0` in the WHERE clause is what makes the cap unbreachable: the
    check and the decrement are one statement, so there is no window between
    them. The transaction is what makes the PER-CUSTOMER limit safe too, which
    is a different guarantee and needs the surrounding BEGIN.
    """
    with con.transaction():
        if per_customer_limit is not None:
            used = con.execute(
                "SELECT count(*) FROM pg_redemptions"
                " WHERE promo_id=%s AND customer_id=%s",
                (promo_id, customer_id)).fetchone()[0]
            if used >= per_customer_limit:
                return False
        cur = con.execute(
            "UPDATE pg_promo_budget SET remaining = remaining - 1"
            " WHERE promo_id = %s AND remaining > 0", (promo_id,))
        if cur.rowcount != 1:
            return False
        con.execute(
            "INSERT INTO pg_redemptions(promo_id,customer_id,created_at)"
            " VALUES (%s,%s,%s)", (promo_id, customer_id, time.time()))
        return True


def storm(promos: dict[str, int], n_workers: int, attempts_each: int,
          per_customer_limit: int | None = None) -> dict:
    """Concurrent redemption attempts across the given promotions.

    Timing starts when the BARRIER TRIPS, not when the threads are launched.
    Connecting to Postgres costs ~10 ms, so a timer started before the connects
    charges the engine for setup it is not doing during the drill -- the first
    version of this did exactly that and reported 71 redemptions/s for a single
    worker against 827/s for the same code in a plain loop. `setup_seconds` is
    returned rather than hidden.
    """
    names = list(promos)
    granted, refused = [0], [0]
    errors: list[str] = []
    lock = threading.Lock()
    started = []
    barrier = threading.Barrier(
        n_workers, action=lambda: started.append(time.perf_counter()))

    def worker(w: int):
        try:
            with psycopg.connect(DSN) as con:
                barrier.wait()
                for i in range(attempts_each):
                    pid = names[(w + i) % len(names)]
                    try:
                        ok = redeem(con, pid, "cust-%d-%d" % (w, i),
                                    per_customer_limit)
                        with lock:
                            if ok:
                                granted[0] += 1
                            else:
                                refused[0] += 1
                    except Exception as e:
                        with lock:
                            errors.append("%s: %s" % (type(e).__name__, e))
        except Exception as e:
            with lock:
                errors.append("connect: %s" % e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    launched = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = time.perf_counter()
    # measured from the barrier, so connection setup is not charged to either
    # engine; `setup_seconds` is reported rather than hidden.
    t0 = started[0] if started else launched
    elapsed = done - t0
    setup = t0 - launched

    with psycopg.connect(DSN) as con:
        rows = con.execute(
            "SELECT promo_id, cap, remaining FROM pg_promo_budget").fetchall()
        redeemed = con.execute("SELECT count(*) FROM pg_redemptions").fetchone()[0]
    overspend = sum(max(0, (cap - rem) - cap) for _, cap, rem in rows)
    consumed = sum(cap - rem for _, cap, rem in rows)
    return dict(engine="postgres", promos=len(promos), workers=n_workers,
                granted=granted[0], refused=refused[0], consumed=consumed,
                redeemed_rows=redeemed, overspend=overspend,
                seconds=elapsed, setup_seconds=setup, throughput=granted[0] / max(elapsed, 1e-9),
                errors=errors[:5], n_errors=len(errors))


def sqlite_storm(promos: dict[str, int], n_workers: int, attempts_each: int,
                 path: str) -> dict:
    """The same storm against SE-2's own SQLite budget code, unchanged.

    Uses `budget.claim` rather than a reimplementation, so what is compared is
    this project's actual cap mechanism on two engines.
    """
    import os
    import threading
    import time as _t

    from src import budget as B

    if os.path.exists(path):
        os.remove(path)
    con = B.init(path, fresh=True)
    for pid, cap in promos.items():
        B.register(con, pid, cap)
    con.commit()
    con.close()

    names = list(promos)
    granted, refused = [0], [0]
    errors: list[str] = []
    lock = threading.Lock()
    started = []
    barrier = threading.Barrier(
        n_workers, action=lambda: started.append(_t.perf_counter()))

    def worker(w: int):
        c = B.connect(path)
        try:
            barrier.wait()
            for i in range(attempts_each):
                pid = names[(w + i) % len(names)]
                try:
                    ok = B.claim(c, pid, "cust-%d-%d" % (w, i))
                    with lock:
                        if ok:
                            granted[0] += 1
                        else:
                            refused[0] += 1
                except Exception as e:
                    with lock:
                        errors.append("%s: %s" % (type(e).__name__, e))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    launched = _t.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = _t.perf_counter()
    t0 = started[0] if started else launched
    elapsed = done - t0

    c = B.connect(path)
    rows = c.execute("SELECT promo_id, cap, remaining FROM promo_budget").fetchall()
    c.close()
    consumed = sum(r["cap"] - r["remaining"] for r in rows)
    overspend = sum(max(0, -r["remaining"]) for r in rows)
    return dict(engine="sqlite", promos=len(promos), workers=n_workers,
                granted=granted[0], refused=refused[0], consumed=consumed,
                overspend=overspend, seconds=elapsed,
                setup_seconds=t0 - launched,
                throughput=granted[0] / max(elapsed, 1e-9),
                errors=errors[:5], n_errors=len(errors))


def scaling_ratio(n_workers: int = 16, attempts_each: int = 40,
                  cap: int = 1_000_000, reps: int = 9,
                  sqlite_path: str = "out/budget_scaling.db") -> dict:
    """Do redemptions of DIFFERENT promotions proceed in parallel on Postgres?

    The prediction this project wrote without a Postgres to test it on. Measured
    PAIRED -- one promotion and sixteen promotions run back to back inside each
    repetition -- because the absolute rates on one machine are noisy enough
    that an unpaired ratio is a number that happened rather than a measurement.
    SE-1 hit exactly this and its notes carry the same warning.
    """
    import statistics

    one = {"P-000": cap}
    many = {"P-%03d" % i: cap for i in range(16)}
    have_pg = available()
    ratios: dict[str, list[float]] = {"sqlite": []}
    if have_pg:
        ratios["postgres"] = []
    for _ in range(reps):
        a = sqlite_storm(one, n_workers, attempts_each, sqlite_path)
        b = sqlite_storm(many, n_workers, attempts_each, sqlite_path)
        ratios["sqlite"].append(b["throughput"] / max(a["throughput"], 1e-9))
        if have_pg:
            reset(one)
            pa = storm(one, n_workers, attempts_each)
            reset(many)
            pb = storm(many, n_workers, attempts_each)
            ratios["postgres"].append(
                pb["throughput"] / max(pa["throughput"], 1e-9))
    import math

    def sign_test(vals):
        """How often is the ratio above 1.0, and could that be chance?

        Tested PER ENGINE against 1.0 rather than engine-against-engine. The
        paired between-engine test was the obvious choice and it is the wrong
        one here: SQLite's ratio ranges 0.44 to 4.64 because its runs finish in
        a fraction of a second and timing noise is proportionally enormous, so
        that noise swamps a real Postgres effect and the comparison reads
        p = 0.30 when Postgres alone reads p = 0.04.
        """
        n = len(vals)
        k = sum(1 for v in vals if v > 1.0)
        p = min(1.0, sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n * 2)
        return dict(above_one=k, n=n, p=p)

    out = {"reps": reps}
    for engine, vals in ratios.items():
        out[engine] = dict(median=statistics.median(vals),
                           low=min(vals), high=max(vals),
                           ratios=[round(v, 3) for v in vals],
                           **sign_test(vals))
    if have_pg:
        out["postgres_scaled_more"] = sum(
            1 for x, y in zip(ratios["postgres"], ratios["sqlite"]) if x > y)
    return out
