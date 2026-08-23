"""Persistence for the promotion catalogue, and why an index makes it harder.

THE GAP
-------
"No API and no persistence for the catalogue. Promotions are in-memory
dataclasses; there is no HTTP surface and no merchant CRUD. (Redemptions ARE
persisted -- that is what src/budget.py is.)"

The parenthetical is the interesting part. Redemptions were persisted because
they are MONEY and a lost redemption is an overspend. The catalogue was not,
because losing it on restart merely means reloading a config -- right up until
there is an index built from it, at which point the catalogue and its index can
disagree, and a promotion a merchant deleted keeps firing.

SO THE STORE OWNS THE INDEX
---------------------------
Every write goes through this class, and every write updates the compiled index
incrementally in the same call. That is not elegance, it is the only arrangement
where the two cannot drift: a caller that can write to the table without touching
the index will eventually be written by someone in a hurry.

WHAT IS STILL SQLITE-SHAPED
---------------------------
Rows are JSON blobs keyed by promo_id, not a normalised schema. A real
merchandising system needs the eligibility fields as columns so a merchant can
ask "which promotions target this category" without scanning -- and that query is
the whole reason a promotions console exists. This stores and serves; it does not
support merchandising search.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .index import CompiledCatalogue
from .model import Eligibility, EffectKind, Promotion, Scope, Stacking

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS promotions (
    promo_id    TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
-- Append-only: every edit is a row, so "who turned this on at 4am" is a query
-- rather than an argument. A promotions console without this is the most
-- reliable source of unresolvable incidents in retail engineering.
CREATE TABLE IF NOT EXISTS promotion_audit (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id    TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('create','update','delete')),
    body        TEXT,
    actor       TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_promo ON promotion_audit(promo_id);
"""


def to_json(p: Promotion) -> str:
    el = p.eligibility
    return json.dumps(dict(
        promo_id=p.promo_id, scope=p.scope.value, kind=p.kind.value,
        percent_bp=p.percent_bp, amount_cents=p.amount_cents,
        bogo=list(p.bogo), tier_threshold_cents=p.tier_threshold_cents,
        stacking=p.stacking.value, stack_class=p.stack_class,
        priority=p.priority, max_redemptions=p.max_redemptions,
        per_customer_limit=p.per_customer_limit,
        eligibility=dict(
            categories=sorted(el.categories), skus=sorted(el.skus),
            min_subtotal_cents=el.min_subtotal_cents, min_qty=el.min_qty,
            segments=sorted(el.segments), first_order_only=el.first_order_only,
            days_of_week=sorted(el.days_of_week),
            starts_at=el.starts_at, ends_at=el.ends_at)))


def from_json(body: str) -> Promotion:
    d = json.loads(body)
    e = d["eligibility"]
    return Promotion(
        promo_id=d["promo_id"], scope=Scope(d["scope"]), kind=EffectKind(d["kind"]),
        percent_bp=d["percent_bp"], amount_cents=d["amount_cents"],
        bogo=tuple(d["bogo"]), tier_threshold_cents=d["tier_threshold_cents"],
        stacking=Stacking(d["stacking"]), stack_class=d["stack_class"],
        priority=d["priority"], max_redemptions=d["max_redemptions"],
        per_customer_limit=d["per_customer_limit"],
        eligibility=Eligibility(
            categories=frozenset(e["categories"]), skus=frozenset(e["skus"]),
            min_subtotal_cents=e["min_subtotal_cents"], min_qty=e["min_qty"],
            segments=frozenset(e["segments"]),
            first_order_only=e["first_order_only"],
            days_of_week=frozenset(e["days_of_week"]),
            starts_at=e["starts_at"], ends_at=e["ends_at"]))


class CatalogueStore:
    """The table and its index, behind one door."""

    def __init__(self, path: str, fresh: bool = False):
        if fresh and os.path.exists(path):
            os.remove(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.index = CompiledCatalogue(self.all())

    # ---- reads ---------------------------------------------------------
    def all(self) -> list[Promotion]:
        return [from_json(r["body"]) for r in
                self.con.execute("SELECT body FROM promotions ORDER BY promo_id")]

    def get(self, promo_id: str) -> Promotion | None:
        r = self.con.execute("SELECT body FROM promotions WHERE promo_id=?",
                             (promo_id,)).fetchone()
        return from_json(r["body"]) if r else None

    def history(self, promo_id: str) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT action, actor, created_at FROM promotion_audit "
            "WHERE promo_id=? ORDER BY audit_id", (promo_id,))]

    # ---- writes --------------------------------------------------------
    def put(self, p: Promotion, actor: str = "merchant") -> str:
        """Create or update. THE INDEX IS UPDATED IN THE SAME CALL, on purpose.

        Any arrangement where a caller can write the row without touching the
        index will eventually be used by someone in a hurry, and the symptom --
        a deleted promotion still firing -- is very hard to reproduce.
        """
        existed = self.get(p.promo_id) is not None
        body = to_json(p)
        now = time.time()
        with self.con:
            self.con.execute(
                "INSERT INTO promotions(promo_id, body, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(promo_id) DO UPDATE SET body=excluded.body, "
                "updated_at=excluded.updated_at", (p.promo_id, body, now))
            self.con.execute(
                "INSERT INTO promotion_audit(promo_id, action, body, actor,"
                " created_at) VALUES (?,?,?,?,?)",
                (p.promo_id, "update" if existed else "create", body, actor, now))
        self.index.add(p)
        return "update" if existed else "create"

    def delete(self, promo_id: str, actor: str = "merchant") -> bool:
        if self.get(promo_id) is None:
            return False
        with self.con:
            self.con.execute("DELETE FROM promotions WHERE promo_id=?", (promo_id,))
            self.con.execute(
                "INSERT INTO promotion_audit(promo_id, action, body, actor,"
                " created_at) VALUES (?,?,NULL,?,?)",
                (promo_id, "delete", actor, time.time()))
        self.index.remove(promo_id)
        return True

    # ---- consistency ---------------------------------------------------
    def index_matches_table(self) -> bool:
        """The invariant the incremental index has to keep.

        Cheap enough to run in a test after every edit sequence, which is where
        it belongs: an index that drifts from its source is worse than no index,
        because it is confidently wrong rather than absent.
        """
        fresh = CompiledCatalogue(self.all())
        return (set(fresh.promos) == set(self.index.promos)
                and fresh.universal == self.index.universal
                and {k: set(v) for k, v in fresh.by_sku.items() if v} ==
                    {k: set(v) for k, v in self.index.by_sku.items() if v}
                and {k: set(v) for k, v in fresh.by_category.items() if v} ==
                    {k: set(v) for k, v in self.index.by_category.items() if v})
