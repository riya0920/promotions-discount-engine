"""The merchant console and the pricing endpoint.

THE GAP
-------
"No API and no persistence for the catalogue."

TWO ENDPOINTS, TWO VERY DIFFERENT LATENCY BUDGETS, AND THAT IS THE DESIGN
-------------------------------------------------------------------------
`POST /price` runs inside a checkout, so it has a hard budget measured in
milliseconds and must never do work proportional to the catalogue. It reads the
compiled index and nothing else.

`POST /promotions` is a merchant editing a rule. It can afford to be slow, and it
is the ONLY path that writes -- so it is where the index is kept in step, in the
same call that writes the row. Any arrangement where those two can drift will
drift.

WHAT A MERCHANT CONSOLE NEEDS THAT THIS HAS
--------------------------------------------
  * a simulator (`/price?at=`) that answers "what would this cart cost on
    Friday", because that is the question merchants actually ask and a flag
    flipped by a cron job cannot answer it;
  * an audit trail on every edit, because "who turned this on at 4am" is
    otherwise unanswerable;
  * an explanation for every promotion that did NOT apply. A promo that vanishes
    silently is the single most common promotions escalation there is.

Run:  uvicorn serve:app --port 8015
"""
from __future__ import annotations

import html
import os
import sys
import time

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import engine as ENG                     # noqa: E402
from src.catalogue_store import CatalogueStore, from_json, to_json  # noqa: E402
from src.currency import format_minor              # noqa: E402
from src.index import evaluate_indexed             # noqa: E402
from src.model import Cart, Line                   # noqa: E402
from src.money import fmt                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(HERE, "out", "catalogue.db")

app = FastAPI(title="SE-2 promotions engine",
              description="Pricing on the checkout path, CRUD on the merchant path.")
STORE = CatalogueStore(STORE_PATH, fresh=True)


def _seed():
    """A small starting catalogue so the console is not empty on first run."""
    from src.model import Eligibility, EffectKind, Promotion, Scope, Stacking
    seeds = [
        Promotion("SAVE10", Scope.ORDER, EffectKind.PERCENT_OFF, percent_bp=1000,
                  eligibility=Eligibility(min_subtotal_cents=5000),
                  stack_class="order", priority=10),
        Promotion("TEE20", Scope.CATEGORY, EffectKind.PERCENT_OFF, percent_bp=2000,
                  eligibility=Eligibility(categories=frozenset({"apparel"})),
                  stack_class="category", priority=20),
        Promotion("FREESHIP", Scope.SHIPPING, EffectKind.FREE_SHIPPING,
                  eligibility=Eligibility(min_subtotal_cents=7500),
                  stack_class="shipping", priority=30),
        Promotion("NEWBIE", Scope.ORDER, EffectKind.AMOUNT_OFF, amount_cents=500,
                  eligibility=Eligibility(first_order_only=True),
                  stacking=Stacking.EXCLUSIVE, priority=5),
    ]
    for p in seeds:
        STORE.put(p, actor="seed")


_seed()


class LineIn(BaseModel):
    sku: str
    category: str
    unit_price_cents: int = Field(..., ge=0)
    qty: int = Field(..., gt=0)
    tax_bp: int = 0


class CartIn(BaseModel):
    lines: list[LineIn]
    shipping_cents: int = 0
    customer_segment: str = "regular"
    is_first_order: bool = False
    day_of_week: int = 0
    currency: str = "USD"
    at: float | None = Field(default=None,
                             description="epoch seconds; simulate a future date")


def _cart(c: CartIn) -> Cart:
    return Cart(tuple(Line(l.sku, l.category, l.unit_price_cents, l.qty, l.tax_bp)
                      for l in c.lines),
                shipping_cents=c.shipping_cents,
                customer_segment=c.customer_segment,
                is_first_order=c.is_first_order, day_of_week=c.day_of_week)


@app.get("/health")
def health():
    return {"ok": True, **STORE.index.stats()}


@app.post("/price")
def price(c: CartIn):
    """The checkout path. Reads the compiled index; never scans the catalogue."""
    cart = _cart(c)
    now = c.at if c.at is not None else time.time()
    t0 = time.perf_counter()
    ev = evaluate_indexed(cart, STORE.index, now=now)
    ms = (time.perf_counter() - t0) * 1000

    candidates = STORE.index.candidates(cart, now=now)
    applied = set(ev.applied)          # Evaluation.applied is promo ids
    # EVERY promotion that did not apply gets a reason. A promo that vanishes
    # silently is the most common promotions escalation there is, and "it did not
    # match" is not an answer a merchant can act on.
    skipped = []
    for p in STORE.all():
        if p.promo_id in applied:
            continue
        if p.promo_id not in {c_.promo_id for c_ in candidates}:
            skipped.append(dict(promo_id=p.promo_id, reason="pruned by index"))
        else:
            ok, why = ENG.is_eligible(p, cart, None, now=now)
            skipped.append(dict(promo_id=p.promo_id,
                                reason=why if not ok else "lost stacking resolution"))
    return {
        "currency": c.currency,
        "subtotal": format_minor(cart.subtotal, c.currency),
        "discount": format_minor(ev.line_discount_total, c.currency),
        "shipping": format_minor(ev.shipping_paid_cents, c.currency),
        "tax": format_minor(ev.tax_cents, c.currency),
        "total": format_minor(ev.total_paid, c.currency),
        "applied": list(ev.applied),
        "not_applied": skipped,
        "candidates_considered": len(candidates),
        "catalogue_size": len(STORE.index.promos),
        "latency_ms": round(ms, 3),
    }


@app.get("/promotions")
def list_promotions():
    return {"promotions": [to_json(p) for p in STORE.all()],
            "index": STORE.index.stats()}


@app.put("/promotions/{promo_id}")
def put_promotion(promo_id: str, body: dict = Body(...), actor: str = "merchant"):
    """Create or update. The index moves in the same call as the row."""
    body["promo_id"] = promo_id
    try:
        p = from_json(__import__("json").dumps(body))
    except Exception as exc:
        raise HTTPException(422, "malformed promotion: %s" % exc)
    action = STORE.put(p, actor=actor)
    return {"action": action, "promo_id": promo_id,
            "index": STORE.index.stats(),
            "index_consistent": STORE.index_matches_table()}


@app.delete("/promotions/{promo_id}")
def delete_promotion(promo_id: str, actor: str = "merchant"):
    if not STORE.delete(promo_id, actor=actor):
        raise HTTPException(404, "no such promotion")
    return {"deleted": promo_id, "index": STORE.index.stats(),
            "index_consistent": STORE.index_matches_table()}


@app.get("/promotions/{promo_id}/history")
def promo_history(promo_id: str):
    """Who changed what, and when. Otherwise unanswerable at 4am."""
    h = STORE.history(promo_id)
    if not h:
        raise HTTPException(404, "no history for %r" % promo_id)
    return {"promo_id": promo_id, "history": h}


@app.get("/", response_class=HTMLResponse)
def console():
    rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%d</td></tr>"
        % (html.escape(p.promo_id), p.scope.value, p.kind.value,
           html.escape(p.stack_class or "-"), p.priority)
        for p in STORE.all())
    st = STORE.index.stats()
    return """<!doctype html><meta charset=utf-8><title>SE-2 console</title>
<style>
 body{font:15px/1.55 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
 table{border-collapse:collapse;margin:1rem 0}
 td,th{border:1px solid #ddd;padding:.35rem .6rem;text-align:left}
 code{background:#f4f4f4;padding:.1rem .3rem}
 .why{color:#555;font-size:13px}
</style>
<h1>SE-2 &mdash; promotions console</h1>
<table><tr><th>promo</th><th>scope</th><th>effect</th><th>stack class</th>
<th>priority</th></tr>%s</table>
<p class=why>Index: %d promotions, %d universal, %d scoped, %d categories,
%d SKUs.</p>

<p class=why><b>Two endpoints, two latency budgets.</b> <code>POST /price</code>
runs inside a checkout and reads the compiled index &mdash; it never does work
proportional to the catalogue. <code>PUT /promotions/{id}</code> is a merchant
editing a rule; it is the only path that writes, and it updates the index in the
same call. Any arrangement where those two can drift, will.</p>

<p class=why><b>Every promotion that did not apply gets a reason</b> in the
<code>/price</code> response. A promo that vanishes silently is the single most
common promotions escalation there is, and &ldquo;it did not match&rdquo; is not
an answer a merchant can act on.</p>

<p class=why><code>/price</code> accepts <code>at</code> as epoch seconds, so a
merchant can ask &ldquo;what would this cart cost on Friday&rdquo;. Scheduling is
evaluated per cart rather than by a cron job flipping a flag &mdash; a flag has a
moment of being wrong, and a timestamp does not.</p>

<p class=why>API: <a href="/docs">/docs</a> &middot;
<a href="/promotions">/promotions</a>.</p>
""" % (rows, st["total"], st["universal"], st["scoped"],
       st["categories_indexed"], st["skus_indexed"])
