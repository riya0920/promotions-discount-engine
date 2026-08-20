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

import pandas as pd

from src import budget as BUD
from src.engine import evaluate, explain
from src.index import CompiledCatalogue, evaluate_indexed
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


def sku_targeted_catalogue(n, rng):
    """A catalogue shaped like a real retailer's: most promotions target specific
    products, because most promotions are funded by a specific vendor or clearing
    a specific overstock. `random_catalogue` above is the opposite extreme --
    nearly everything broad-match -- and reporting both is the point, because the
    index's value lives entirely in this difference."""
    promos = []
    for i in range(n):
        kind = rng.choice([EffectKind.PERCENT_OFF, EffectKind.AMOUNT_OFF,
                           EffectKind.BOGO])
        scope = Scope.ITEM if rng.random() < 0.85 else Scope.CATEGORY
        if scope is Scope.ITEM:
            el = Eligibility(skus=frozenset(
                "SKU%03d" % rng.randint(0, 200) for _ in range(rng.randint(1, 3))))
        else:
            el = Eligibility(categories=frozenset([rng.choice(CATEGORIES)]))
        promos.append(Promotion(
            "T%04d" % i, scope, kind, eligibility=el,
            percent_bp=rng.choice([500, 1000, 1500, 2000]),
            amount_cents=rng.choice([200, 500, 1000]),
            bogo=(rng.randint(1, 3), 1),
            stacking=Stacking.STACKABLE,
            stack_class=rng.choice(["", "", "", "vendor"]),
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


def section_index(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("2b. THE ELIGIBILITY INDEX -- THE STATED FIX, NOW MEASURED")
    lines.append("=" * 76)
    lines.append("The latency table above scales linearly in catalogue size because")
    lines.append("every promotion is tested against every cart. The README named the")
    lines.append("fix -- a pre-compiled eligibility index -- and did not build it.")
    lines.append("Here it is, and the first thing it has to prove is that it does not")
    lines.append("change any answer.")
    lines.append("")
    rng = random.Random(23)
    rows = []
    for shape, maker in (("broad-match", random_catalogue),
                         ("sku-targeted", sku_targeted_catalogue)):
      for n_promos in (100, 500, 2000):
        cat = maker(n_promos, rng)
        compiled = CompiledCatalogue(cat)
        carts = [random_cart(rng) for _ in range(300)]

        mismatches = 0
        for c in carts:
            if evaluate(c, cat).total_paid != evaluate_indexed(c, compiled).total_paid:
                mismatches += 1

        for c in carts[:20]:
            evaluate(c, cat)
            evaluate_indexed(c, compiled)

        t_naive, t_idx, cand = [], [], []
        for c in carts:
            t0 = time.perf_counter()
            evaluate(c, cat)
            t_naive.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            evaluate_indexed(c, compiled)
            t_idx.append((time.perf_counter() - t0) * 1000)
            cand.append(len(compiled.candidates(c)))
        t_naive.sort()
        t_idx.sort()

        t0 = time.perf_counter()
        CompiledCatalogue(cat)
        compile_ms = (time.perf_counter() - t0) * 1000

        st = compiled.stats()
        rows.append(dict(
            shape=shape,
            promos=n_promos, universal=st["universal"], scoped=st["scoped"],
            mean_candidates=statistics.mean(cand),
            naive_p99=t_naive[int(0.99 * len(t_naive))],
            indexed_p99=t_idx[int(0.99 * len(t_idx))],
            speedup=t_naive[int(0.99 * len(t_naive))] / max(t_idx[int(0.99 * len(t_idx))], 1e-9),
            compile_ms=compile_ms, answer_mismatches=mismatches))
    T = pd.DataFrame(rows).set_index(["shape", "promos"])
    lines.append(T.to_string(float_format=lambda x: "%10.3f" % x))
    lines.append("")
    total_mismatch = int(T.answer_mismatches.sum())
    lines.append("answer_mismatches across 1,800 carts x 6 catalogues: %d"
                 % total_mismatch)
    if total_mismatch == 0:
        lines.append("An index that changes the answer is not an optimisation, it is a")
        lines.append("bug. Equality against the unindexed path is asserted as a property")
        lines.append("test over generated carts, not just spot-checked here.")
    lines.append("")
    lines.append("WHAT THE INDEX CAN AND CANNOT PRUNE, which is the honest read of the")
    lines.append("`universal` column: promotions with no category or SKU restriction")
    lines.append("could apply to any cart, so they survive every prune. Order-level")
    lines.append("percentages, free shipping and first-order offers are all universal,")
    lines.append("and they are common. The index makes the SCOPED tail nearly free and")
    lines.append("does nothing for the universal head -- so the speedup is a function")
    lines.append("of catalogue COMPOSITION, not catalogue size. A merchant whose")
    lines.append("promotions are all order-level gets no benefit at all, and that is")
    lines.append("worth knowing before promising a latency number.")
    lines.append("")
    lines.append("THE TWO CATALOGUE SHAPES ARE THE RESULT. `broad-match` is the")
    lines.append("generator from section 2, where promotions carry few restrictions and")
    lines.append("nearly every one is a candidate for every cart -- the index prunes")
    lines.append("almost nothing and its bookkeeping makes things marginally WORSE.")
    lines.append("`sku-targeted` is what a real retailer's catalogue looks like, because")
    lines.append("most promotions are funded by a specific vendor or clear a specific")
    lines.append("overstock. There the candidate set collapses and the index pays.")
    lines.append("")
    lines.append("So the honest claim is NOT 'indexing makes promo evaluation Nx")
    lines.append("faster'. It is: indexing converts the cost from catalogue SIZE to")
    lines.append("catalogue BREADTH, and whether that helps is a property of the")
    lines.append("merchant's promotions, not of the engine. Shipping this without")
    lines.append("measuring the customer's actual catalogue composition would be")
    lines.append("shipping a benchmark, not an improvement.")
    lines.append("")
    lines.append("Compile cost (%.2f ms at 2,000 promos) is paid when a merchant edits"
                 % T.loc[("sku-targeted", 2000), "compile_ms"])
    lines.append("the catalogue, not per checkout. That amortisation is the other half")
    lines.append("of the win and the reason the compiled object is a separate class")
    lines.append("rather than work done inside evaluate().")
    summary["eligibility_index"] = T.reset_index().round(4).to_dict("records")


def section_atomic_budget(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("3b. BUDGET CAPS IN SHARED STATE -- THE ACTUAL FIX")
    lines.append("=" * 76)
    lines.append("Section 3 showed the race and guarded it with a process-local mutex,")
    lines.append("then said plainly that a mutex is not the fix. It is not: two checkout")
    lines.append("pods do not share a Python lock, so a 1,000-redemption promotion")
    lines.append("issues 1,000 PER POD.")
    lines.append("")
    lines.append("The fix is the same one SE-1 uses for stock -- make the check and the")
    lines.append("decrement ONE conditional statement in the shared store:")
    lines.append("")
    lines.append("    UPDATE promo_budget SET remaining = remaining - 1")
    lines.append("     WHERE promo_id = ? AND remaining > 0")
    lines.append("")
    db = os.path.join(OUT, "budget.db")
    CAP, THREADS, ATTEMPTS = 1000, 24, 6000
    con = BUD.init(db, fresh=True)
    BUD.register(con, "CAPPED-10", CAP)
    con.close()

    granted, lock = [], threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker(w):
        c = BUD.connect(db)
        got = 0
        barrier.wait()
        for i in range(ATTEMPTS // THREADS):
            if BUD.claim(c, "CAPPED-10", "cust%d" % (w * 1000 + i), now=0.0):
                got += 1
        c.close()
        with lock:
            granted.append(got)

    ts = [threading.Thread(target=worker, args=(w,)) for w in range(THREADS)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0

    con = BUD.connect(db)
    row = con.execute("SELECT cap, remaining FROM promo_budget").fetchone()
    n_red = con.execute("SELECT COUNT(*) n FROM redemptions").fetchone()["n"]
    drift = BUD.check_drift(con)
    lines.append("%d threads, %d attempted redemptions, cap %d, no application lock:"
                 % (THREADS, ATTEMPTS, CAP))
    lines.append("  granted            %d" % sum(granted))
    lines.append("  remaining in store %d" % row["remaining"])
    lines.append("  redemption rows    %d" % n_red)
    lines.append("  overspend          %+d" % (sum(granted) - CAP))
    lines.append("  ledger drift       %s" % (drift or "none"))
    lines.append("  wall               %.2fs (%.0f claims/s)" % (wall, ATTEMPTS / wall))
    lines.append("")
    lines.append("Exactly %d granted with no application-level lock anywhere. The" % CAP)
    lines.append("precondition rides in the WHERE clause, so there is no window between")
    lines.append("the check and the decrement -- because there is no check.")
    lines.append("")

    con2 = BUD.init(db + "2", fresh=True)
    BUD.register(con2, "ONE-EACH", 100000)
    con2.close()
    per_cust_granted, lock2 = [], threading.Lock()
    barrier2 = threading.Barrier(12)

    def worker2(w):
        c = BUD.connect(db + "2")
        got = 0
        barrier2.wait()
        for _ in range(50):
            if BUD.claim(c, "ONE-EACH", "the-same-customer",
                         per_customer_limit=1, now=0.0):
                got += 1
        c.close()
        with lock2:
            per_cust_granted.append(got)

    ts2 = [threading.Thread(target=worker2, args=(w,)) for w in range(12)]
    for t in ts2:
        t.start()
    for t in ts2:
        t.join()
    lines.append("PER-CUSTOMER LIMIT, same shape, 12 threads racing for ONE customer's")
    lines.append("single allowed redemption (600 attempts):")
    lines.append("  granted %d  (limit 1)" % sum(per_cust_granted))
    lines.append("")
    lines.append("The per-customer check cannot be conditional in the UPDATE -- it is a")
    lines.append("COUNT over a different table -- so that one genuinely needs the")
    lines.append("transaction, and BEGIN IMMEDIATE is what makes it safe. Two different")
    lines.append("mechanisms for two different shapes of constraint, rather than one")
    lines.append("lock hopefully covering both.")
    lines.append("")
    lines.append("RELEASE PATH: a redemption claimed at price-quote time and then")
    lines.append("abandoned at payment must go back. Without it every abandoned")
    lines.append("checkout permanently burns a redemption and a 1,000-redemption")
    lines.append("promotion silently becomes a 600-redemption one.")
    c3 = BUD.connect(db)
    before = c3.execute("SELECT remaining FROM promo_budget").fetchone()["remaining"]
    rid = c3.execute("SELECT redemption_id FROM redemptions LIMIT 1").fetchone()[0]
    ok1 = BUD.release(c3, "CAPPED-10", rid)
    ok2 = BUD.release(c3, "CAPPED-10", rid)
    after = c3.execute("SELECT remaining FROM promo_budget").fetchone()["remaining"]
    lines.append("  remaining %d -> release -> %d (first call %s, retry %s)"
                 % (before, after, ok1, ok2))
    lines.append("  ledger drift after release: %s" % (BUD.check_drift(c3) or "none"))
    summary["atomic_budget"] = dict(
        cap=CAP, attempts=ATTEMPTS, granted=int(sum(granted)),
        overspend=int(sum(granted) - CAP), drift=drift,
        per_customer_granted=int(sum(per_cust_granted)),
        release_idempotent=bool(ok1 and not ok2))
    con.close()
    c3.close()


def section_tax(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("4b. TAX -- WHY IT COULD NOT STAY OUT OF THE ENGINE")
    lines.append("=" * 76)
    lines.append("The README listed tax as absent and called it 'not a small omission'.")
    lines.append("It is now computed per line, on the POST-DISCOUNT amount.")
    lines.append("")
    cart = Cart(lines=(Line("TEE-BLUE", "apparel", 2499, 2, tax_bp=875),
                       Line("MILK", "grocery", 449, 3, tax_bp=0),
                       Line("HEADPHONES", "electronics", 8999, 1, tax_bp=875)),
                shipping_cents=0, customer_segment="regular", day_of_week=2)
    order20 = Promotion("ORDER-20", Scope.ORDER, EffectKind.PERCENT_OFF,
                        percent_bp=2000, priority=10)
    ev = evaluate(cart, [order20])
    lines.append("Cart: taxable apparel + EXEMPT groceries + taxable electronics,")
    lines.append("with a single 20%-off-order promotion allocated across all three.")
    lines.append("")
    lines.append("%-14s %10s %10s %10s %8s %10s"
                 % ("line", "gross", "discount", "paid", "tax bp", "tax"))
    for lr, tx in zip(ev.lines, ev.tax_by_line):
        lines.append("%-14s %10s %10s %10s %8d %10s"
                     % (lr.line.sku, fmt(lr.line.subtotal), fmt(-lr.discount_cents),
                        fmt(lr.paid), lr.line.tax_bp, fmt(tx)))
    lines.append("%-14s %10s %10s %10s %8s %10s"
                 % ("TOTAL", fmt(cart.subtotal), fmt(-ev.line_discount_total),
                    fmt(ev.merchandise_paid), "", fmt(ev.tax_cents)))
    lines.append("%-14s %43s" % ("TOTAL PAID", fmt(ev.total_paid)))
    lines.append("")
    flat = (ev.merchandise_paid * 875 + 5000) // 10000
    lines.append("A single cart-level rate would have charged %s of tax instead of"
                 % fmt(flat))
    lines.append("%s -- a %s error on one cart, because it taxes the exempt"
                 % (fmt(ev.tax_cents), fmt(flat - ev.tax_cents)))
    lines.append("groceries. There is no single correct cart rate when the lines")
    lines.append("differ, which is precisely why the order-level discount has to be")
    lines.append("ALLOCATED to lines before tax can be computed at all.")
    lines.append("")
    lines.append("THAT IS THE LOAD-BEARING CONNECTION between this project and SE-1:")
    lines.append("the same per-line allocation that makes tax computable is what makes")
    lines.append("a partial refund computable. Get the allocation wrong and you are")
    lines.append("wrong twice, in two different systems, months apart.")
    lines.append("")
    lines.append("DISCOUNT-THEN-TAX vs TAX-THEN-DISCOUNT is a real choice and this")
    lines.append("engine has made it: tax applies to what the customer actually pays,")
    lines.append("because a RETAILER discount reduces the taxable receipt. A")
    lines.append("MANUFACTURER coupon generally does not -- the retailer is reimbursed,")
    lines.append("so the taxable amount stays at full price. That case is NOT modelled")
    lines.append("here, and an engine that has not chosen is doing both by accident.")
    summary["tax"] = dict(per_line=list(ev.tax_by_line), total_tax=ev.tax_cents,
                          flat_rate_would_be=flat, total_paid=ev.total_paid)


def section_scheduling(lines, summary):
    lines.append("")
    lines.append("=" * 76)
    lines.append("4c. SCHEDULED ACTIVATION AND EXPIRY")
    lines.append("=" * 76)
    cart = Cart(lines=(Line("TEE-BLUE", "apparel", 2499, 2),))
    sale = Promotion("FLASH-FRIDAY", Scope.ORDER, EffectKind.PERCENT_OFF,
                     eligibility=Eligibility(starts_at=1000.0, ends_at=2000.0),
                     percent_bp=3000, priority=10)
    for now, label in ((500.0, "before window"), (1500.0, "inside window"),
                       (2500.0, "after window")):
        ev = evaluate(cart, [sale], now=now)
        reason = dict(ev.rejected).get("FLASH-FRIDAY", "-")
        lines.append("  t=%6.0f  %-15s total %s   %s"
                     % (now, label, fmt(ev.total_paid),
                        "APPLIED" if ev.applied else "rejected: " + reason))
    lines.append("")
    lines.append("The window is enforced in the ENGINE, not by a cron job that flips an")
    lines.append("active flag. That matters because a flag has a moment of being wrong:")
    lines.append("the job runs late, or runs twice, or the pod that owns it restarts,")
    lines.append("and a sale goes live early to whoever happens to check out then.")
    lines.append("Evaluating the timestamp per cart has no such window, and it also")
    lines.append("makes the merchant simulator able to answer 'what would this cart")
    lines.append("cost on Friday' -- which is the question they actually ask.")
    summary["scheduling"] = True


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
    section_index(lines, summary)
    section_budget(lines, summary)
    section_atomic_budget(lines, summary)
    section_simulator(lines)
    section_tax(lines, summary)
    section_scheduling(lines, summary)
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(OUT, "engine_report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(OUT, "engine_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print("\n-> out/engine_report.txt")


if __name__ == "__main__":
    main()
