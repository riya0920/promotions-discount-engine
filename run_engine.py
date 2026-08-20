"""Three things the property suite cannot show: a trace, a latency budget, and a
budget cap under concurrency.

Checkout calls promotion evaluation SYNCHRONOUSLY. It is not a batch job, it has
a latency budget, and if the promo engine misses that budget the whole checkout
does. So the engine is measured against a stated budget with a realistic
catalogue size rather than demoed on one cart.

The budget-cap section is the SE-1 inventory problem wearing a different hat: a
promotion with 1,000 redemptions must issue exactly 1,000 across concurrent
checkouts, and getting that wrong gives away margin that nobody authorised.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import threading
import time

from src.engine import evaluate, explain
from src.model import (Cart, Eligibility, EffectKind, Line, Promotion, Scope,
                       Stacking)
from src.money import fmt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
CHECKOUT_BUDGET_MS = 25.0     # the slice checkout gives promotions
CATEGORIES = ["grocery", "apparel", "home", "electronics", "toys"]


def demo_cart():
    return Cart(lines=(Line("TEE-BLUE", "apparel", 2499, 3),
                       Line("MUG", "home", 1250, 2),
                       Line("COFFEE-1KG", "grocery", 1899, 1)),
                shipping_cents=799, customer_segment="vip", is_first_order=False,
                day_of_week=5)


def demo_promos():
    return [
        Promotion("BOGO-TEE", Scope.ITEM, EffectKind.BOGO,
                  eligibility=Eligibility(skus=frozenset({"TEE-BLUE"})),
                  bogo=(2, 1), priority=10),
        Promotion("HOME-15", Scope.CATEGORY, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(categories=frozenset({"home"})),
                  percent_bp=1500, priority=20),
        Promotion("ORDER-20", Scope.ORDER, EffectKind.PERCENT_OFF,
                  percent_bp=2000, priority=30, stack_class="seasonal"),
        Promotion("SEASONAL-10OFF", Scope.ORDER, EffectKind.AMOUNT_OFF,
                  amount_cents=1000, priority=40, stack_class="seasonal"),
        Promotion("SHIP-FREE-50", Scope.SHIPPING, EffectKind.FREE_SHIPPING,
                  eligibility=Eligibility(min_subtotal_cents=5000), priority=50),
        Promotion("FIRST-ORDER-25", Scope.ORDER, EffectKind.PERCENT_OFF,
                  eligibility=Eligibility(first_order_only=True),
                  percent_bp=2500, stacking=Stacking.EXCLUSIVE, priority=5),
    ]


def random_catalogue(n, rng):
    promos = []
    for i in range(n):
        kind = rng.choice([EffectKind.PERCENT_OFF, EffectKind.AMOUNT_OFF,
                           EffectKind.BOGO, EffectKind.TIERED_SPEND,
                           EffectKind.FREE_SHIPPING])
        scope = (Scope.SHIPPING if kind is EffectKind.FREE_SHIPPING
                 else rng.choice([Scope.ITEM, Scope.CATEGORY, Scope.ORDER]))
        promos.append(Promotion(
            "P%03d" % i, scope, kind,
            eligibility=Eligibility(
                categories=frozenset(rng.sample(CATEGORIES, rng.randint(0, 2))),
                min_subtotal_cents=rng.choice([0, 0, 2500, 5000, 10000]),
                min_qty=rng.choice([0, 0, 2, 3]),
            ),
            percent_bp=rng.choice([500, 1000, 1500, 2000]),
            amount_cents=rng.choice([200, 500, 1000, 2500]),
            bogo=(rng.randint(1, 3), 1),
            tier_threshold_cents=rng.choice([0, 5000, 10000]),
            stacking=rng.choice([Stacking.STACKABLE] * 9 + [Stacking.EXCLUSIVE]),
            stack_class=rng.choice(["", "", "", "seasonal", "loyalty"]),
            priority=rng.randint(1, 200)))
    return promos


def random_cart(rng):
    n = rng.randint(1, 8)
    lines = tuple(Line("SKU%03d" % rng.randint(0, 200),
                       rng.choice(CATEGORIES),
                       rng.randint(199, 9999), rng.randint(1, 4))
                  for _ in range(n))
    return Cart(lines=lines, shipping_cents=rng.choice([0, 499, 799]),
                customer_segment=rng.choice(["regular", "vip"]),
                is_first_order=rng.random() < 0.2,
                day_of_week=rng.randint(0, 6))


# ==========================================================================
def section_trace(lines):
    lines.append("=" * 76)
    lines.append("1. EXPLANATION TRACE  (the CS-agent and merchant-debug surface)")
    lines.append("=" * 76)
    cart, promos = demo_cart(), demo_promos()
    ev = evaluate(cart, promos)
    lines.append(explain(ev, cart))
    lines.append("")
    lines.append("Note SEASONAL-10OFF: rejected, not silently dropped. It shares the")
    lines.append("'seasonal' stacking class with ORDER-20, which sorted first. Every")
    lines.append("promotion in the catalogue is either applied or carries a reason --")
    lines.append("that is asserted as a property, because a promo that vanishes without")
    lines.append("explanation is the single most common promotions support escalation.")
    lines.append("")
    lines.append("Reconciliation check on this cart:")
    lines.append("  sum of line discounts   %s" % fmt(ev.line_discount_total))
    lines.append("  subtotal - paid         %s" % fmt(cart.subtotal - ev.merchandise_paid))
    assert ev.line_discount_total == cart.subtotal - ev.merchandise_paid
    lines.append("  exact to the cent: yes")
    return ev


def section_latency(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("2. EVALUATION LATENCY vs THE CHECKOUT BUDGET")
    lines.append("=" * 76)
    rng = random.Random(11)
    rows = []
    for n_promos in (10, 50, 100, 250, 500):
        cat = random_catalogue(n_promos, rng)
        carts = [random_cart(rng) for _ in range(400)]
        # warm
        for c in carts[:20]:
            evaluate(c, cat)
        times = []
        for c in carts:
            t0 = time.perf_counter()
            evaluate(c, cat)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        rows.append(dict(active_promos=n_promos,
                         p50_ms=statistics.median(times),
                         p95_ms=times[int(0.95 * len(times))],
                         p99_ms=times[int(0.99 * len(times))],
                         max_ms=times[-1]))
    lines.append("%14s %9s %9s %9s %9s %10s" %
                 ("active promos", "p50 ms", "p95 ms", "p99 ms", "max ms", "vs budget"))
    for r in rows:
        verdict = "OK" if r["p99_ms"] <= CHECKOUT_BUDGET_MS else "OVER"
        lines.append("%14d %9.3f %9.3f %9.3f %9.3f %10s"
                     % (r["active_promos"], r["p50_ms"], r["p95_ms"],
                        r["p99_ms"], r["max_ms"], verdict))
    lines.append("")
    lines.append("Checkout budget for promotions: %.0f ms at p99." % CHECKOUT_BUDGET_MS)
    lines.append("")
    lines.append("Evaluation is O(promos x lines) with no index: every promotion in the")
    lines.append("catalogue is tested for eligibility against every cart. That is honest")
    lines.append("about where this would break -- the scaling is linear in catalogue")
    lines.append("size, so the number above is a measurement of THIS implementation, not")
    lines.append("a claim about promotion engines. The fix when it stops fitting is a")
    lines.append("pre-compiled eligibility index (category -> candidate promos) plus")
    lines.append("caching of the compiled catalogue; neither is built here.")
    summary["latency"] = rows
    return rows


def _run_concurrent(promo, n_threads, n_checkouts, locked):
    """Every thread races to redeem the same capped promotion."""
    redemptions = {promo.promo_id: 0}
    lock = threading.Lock()
    granted = []

    def worker():
        local = 0
        for _ in range(n_checkouts // n_threads):
            cart = Cart(lines=(Line("SKU1", "grocery", 5000, 1),))
            if locked:
                with lock:
                    ev = evaluate(cart, [promo], redemptions)
                    if promo.promo_id in ev.applied:
                        redemptions[promo.promo_id] += 1
                        local += 1
            else:
                # the naive version: check and increment without holding a lock
                ev = evaluate(cart, [promo], redemptions)
                if promo.promo_id in ev.applied:
                    cur = redemptions[promo.promo_id]
                    time.sleep(0)          # widen the window the race needs
                    redemptions[promo.promo_id] = cur + 1
                    local += 1
        granted.append(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(granted)


def section_budget(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("3. BUDGET CAPS UNDER CONCURRENCY")
    lines.append("=" * 76)
    CAP = 1000
    promo = Promotion("CAPPED-10", Scope.ORDER, EffectKind.PERCENT_OFF,
                      percent_bp=1000, max_redemptions=CAP)
    rows = []
    for locked in (False, True):
        got = _run_concurrent(promo, n_threads=16, n_checkouts=4000, locked=locked)
        rows.append(dict(guarded=locked, cap=CAP, granted=got, overspend=got - CAP))
    lines.append("%-28s %8s %10s %12s" % ("check-and-increment", "cap", "granted", "overspend"))
    for r in rows:
        lines.append("%-28s %8d %10d %+12d"
                     % ("under lock" if r["guarded"] else "unsynchronised (naive)",
                        r["cap"], r["granted"], r["overspend"]))
    lines.append("")
    lines.append("16 threads, 4,000 attempted redemptions against a 1,000 cap.")
    lines.append("")
    lines.append("The naive version is the same defect as decrement-and-hope inventory:")
    lines.append("eligibility is CHECKED and the counter is INCREMENTED in two steps, so")
    lines.append("two threads both read 999 and both redeem. Every redemption past the")
    lines.append("cap is margin given away that no merchant authorised.")
    lines.append("")
    lines.append("HONEST LIMIT OF THIS DEMONSTRATION: the guarded version holds a")
    lines.append("process-local mutex, which is only correct because this is one process.")
    lines.append("A real deployment needs the check and the increment to be one atomic")
    lines.append("operation in shared state -- a conditional UPDATE with a WHERE clause on")
    lines.append("the remaining count, the same mechanism SE-1 uses for stock. The mutex")
    lines.append("here demonstrates the failure and the shape of the fix, not the fix.")
    summary["budget_caps"] = rows


def section_simulator(lines):
    lines.append("")
    lines.append("=" * 76)
    lines.append("4. MERCHANT DRAFT-PROMO SIMULATOR")
    lines.append("=" * 76)
    lines.append("'What would this cart cost if I shipped this draft?' -- the tool every")
    lines.append("promo team asks for, so they can see the interaction before customers do.")
    lines.append("")
    cart = demo_cart()
    live = demo_promos()
    draft = Promotion("DRAFT-BOGO-MUG", Scope.ITEM, EffectKind.BOGO,
                      eligibility=Eligibility(skus=frozenset({"MUG"})),
                      bogo=(1, 1), priority=15)
    before = evaluate(cart, live)
    after = evaluate(cart, live + [draft])
    lines.append("  without draft   %s" % fmt(before.total_paid))
    lines.append("  with draft      %s" % fmt(after.total_paid))
    lines.append("  delta           %s" % fmt(after.total_paid - before.total_paid))
    lines.append("")
    lines.append("  applied without: %s" % ", ".join(before.applied))
    lines.append("  applied with:    %s" % ", ".join(after.applied))
    newly_rejected = set(dict(after.rejected)) - set(dict(before.rejected))
    lines.append("  newly rejected because of the draft: %s"
                 % (", ".join(sorted(newly_rejected)) or "none"))
    lines.append("")
    lines.append("That last line is the point of the tool. A draft promotion does not")
    lines.append("only add its own discount -- it can knock an existing promotion out of")
    lines.append("the cart through exclusivity or a shared stacking class, and the")
    lines.append("merchant needs to see that before the sale starts, not after.")


def main():
    os.makedirs(OUT, exist_ok=True)
    lines, summary = [], {}
    section_trace(lines)
    section_latency(lines, summary)
    section_budget(lines, summary)
    section_simulator(lines)
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(OUT, "engine_report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(OUT, "engine_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print("\n-> out/engine_report.txt")


if __name__ == "__main__":
    main()
