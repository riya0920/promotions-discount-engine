"""Budget caps enforced in SHARED STATE, not by a process-local mutex.

WHAT WAS WRONG WITH THE MUTEX
-----------------------------
The first version guarded check-and-increment with `threading.Lock`. That is
correct for one process and useless for the deployment it stands in for: two
checkout pods do not share a Python lock, so the cap is per-pod and a promotion
capped at 1,000 issues 1,000 PER POD.

The fix is the same one SE-1 uses for stock, and it is worth stating that they
are the same problem: make the CHECK and the DECREMENT one atomic operation in
the shared store.

    UPDATE promo_budget
       SET remaining = remaining - 1
     WHERE promo_id = ? AND remaining > 0

A caller that gets rowcount 1 holds a redemption. A caller that gets rowcount 0
lost, changed nothing, and knows it. There is no window between the check and
the decrement because there is no check -- the precondition rides in the WHERE
clause.

PER-CUSTOMER LIMITS use the same shape against a different key, which is why
they live here rather than in the engine: the engine can READ a limit to decide
eligibility, but only the store can ATOMICALLY claim one.

HONEST LIMIT: SQLite serialises writers, so this proves the ALGORITHM is
race-free without proving it scales. On Postgres the same statement takes a row
lock and concurrent redemptions of DIFFERENT promotions proceed in parallel;
here they queue. What transfers is the conditional-UPDATE shape, not throughput.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=15000;

CREATE TABLE IF NOT EXISTS promo_budget (
    promo_id   TEXT PRIMARY KEY,
    cap        INTEGER NOT NULL,
    remaining  INTEGER NOT NULL CHECK (remaining >= 0)
);

-- Append-only: one row per granted redemption. The count of rows here must
-- always equal cap - remaining, which is the drift check below.
CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id      TEXT NOT NULL,
    customer_id   TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_red_promo_cust
    ON redemptions(promo_id, customer_id);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(path: str, fresh: bool = True) -> sqlite3.Connection:
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    con = connect(path)
    con.executescript(SCHEMA)
    return con


def register(con: sqlite3.Connection, promo_id: str, cap: int) -> None:
    con.execute("INSERT OR REPLACE INTO promo_budget(promo_id, cap, remaining)"
                " VALUES (?,?,?)", (promo_id, cap, cap))


def claim(con: sqlite3.Connection, promo_id: str, customer_id: str,
          per_customer_limit: int | None = None, now: float = 0.0) -> bool:
    """Atomically claim one redemption. True = granted.

    The whole thing is one transaction so the per-customer check and the budget
    decrement cannot interleave with another claimer. The budget decrement is
    itself conditional, so even without the transaction the cap could not be
    breached -- the transaction is what makes the PER-CUSTOMER limit safe too.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        if per_customer_limit is not None:
            used = con.execute(
                "SELECT COUNT(*) n FROM redemptions WHERE promo_id=? AND customer_id=?",
                (promo_id, customer_id)).fetchone()["n"]
            if used >= per_customer_limit:
                con.execute("ROLLBACK")
                return False

        cur = con.execute(
            "UPDATE promo_budget SET remaining = remaining - 1"
            " WHERE promo_id = ? AND remaining > 0", (promo_id,))
        if cur.rowcount != 1:
            con.execute("ROLLBACK")
            return False

        con.execute("INSERT INTO redemptions(promo_id, customer_id, created_at)"
                    " VALUES (?,?,?)", (promo_id, customer_id, now))
        con.execute("COMMIT")
        return True
    except Exception:
        con.execute("ROLLBACK")
        raise


def release(con: sqlite3.Connection, promo_id: str, redemption_id: int) -> bool:
    """Give a redemption back -- checkout failed after the promo was claimed.

    Without this, every abandoned checkout permanently burns a redemption and a
    1,000-redemption promotion silently becomes a 600-redemption one. Guarded on
    the row still existing so a retry cannot refund it twice.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        cur = con.execute("DELETE FROM redemptions WHERE redemption_id=? AND promo_id=?",
                          (redemption_id, promo_id))
        if cur.rowcount != 1:
            con.execute("ROLLBACK")
            return False
        con.execute("UPDATE promo_budget SET remaining = remaining + 1"
                    " WHERE promo_id = ? AND remaining < cap", (promo_id,))
        con.execute("COMMIT")
        return True
    except Exception:
        con.execute("ROLLBACK")
        raise


def check_drift(con: sqlite3.Connection) -> list[str]:
    """cap - remaining must equal the number of redemption rows, always."""
    problems = []
    for r in con.execute(
            "SELECT b.promo_id, b.cap, b.remaining,"
            " (SELECT COUNT(*) FROM redemptions r WHERE r.promo_id=b.promo_id) AS granted"
            " FROM promo_budget b"):
        if r["cap"] - r["remaining"] != r["granted"]:
            problems.append(
                "%s: cap %d remaining %d implies %d granted, ledger has %d"
                % (r["promo_id"], r["cap"], r["remaining"],
                   r["cap"] - r["remaining"], r["granted"]))
        if r["remaining"] < 0:
            problems.append("%s: negative remaining %d" % (r["promo_id"], r["remaining"]))
    return problems
