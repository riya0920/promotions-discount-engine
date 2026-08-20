# SE-2 — Promotions & Discount Engine

**This is not deployable.** It is the first ~20% of the spec: the piece the
hiring-manager doc calls the differentiator — property-based testing aimed at
rule *interactions* — built and run, with the missing 80% named at the bottom.

```bash
python -m pytest tests -q   # 15 tests, ~70s (Hypothesis: 400 examples each)
python run_engine.py        # trace, latency table, budget-cap race, draft simulator
```

Read [BUGS_FOUND.md](BUGS_FOUND.md) first. It is the actual deliverable.

## Why property testing here

Promotions look like if-statements and are a combinatorial correctness problem.
Stacking, application order, allocation and rounding interact, and the
money-losing cases live in interactions nobody thinks to write an example for.
So carts *and* promotions are both generated, and the assertions are invariants:

| property | holds? |
|---|---|
| total ≥ 0, no line discounted below zero | ✅ |
| configurable price floor respected | ✅ |
| Σ line discounts == order discount, exactly | ✅ |
| same cart + promos → same result | ✅ |
| every promo is applied **or** carries a rejection reason | ✅ |
| trace totals equal the evaluation | ✅ |
| adding a promo never raises the total | ✅ *only* for genuinely combinable promos |
| removing an item never raises what remains pays | ✅ *only* excluding BOGO + thresholds |

Both qualifications were forced by counterexamples, not chosen for convenience.
That is the point — see below.

## The two findings

**A promotion worth nothing made a cart $5.00 more expensive.** A 0%-off ITEM
promo sorts ahead of a 20%-off CATEGORY promo, claims the shared `seasonal`
stacking class, and locks the valuable one out. Resolution picks the winner by
**sort order, not by value.**

My first version of that property excluded only `EXCLUSIVE` promos, assuming
exclusivity was the only mutual-exclusion mechanism. Hypothesis found the same
bug through stacking classes instead, which generalises it: *any* mutual-exclusion
mechanism resolved by priority breaks catalogue monotonicity. Exclusivity was not
special, it was just the one I had thought of.

Resolved as a **policy choice, not an arithmetic defect**: priority-greedy stays
the default (a merchant is entitled to say "this one wins even though it's worth
less"), and `best_of_resolution()` is implemented as the alternative with a
second property asserting monotonicity *does* hold under it.

**Removing an item raised the price of what remained** — two 1¢ units with
buy-1-get-1, delete the paid one, and the survivor stops being free. Here the
**property was wrong, not the engine**: any promotion qualifying on the *other*
items in the cart necessarily breaks monotonicity under removal. That is what
BOGO means. The generator now excludes BOGO and thresholds, and both exclusions
are documented as findings.

Telling those two apart — change the code vs change the property — is the actual
skill the suite is testing for.

## Money is integer cents

No floats anywhere. Percentages apply in basis points with **half-to-even**
rounding (half-up is biased upward by half a cent per tie, at the merchant's
expense). Order-level discounts are **allocated down to line items** by
largest-remainder apportionment, so `Σ line discounts == order discount` exactly
— which is the invariant a partial refund downstream stands on. The alternative
(residual cent always to line 1) is deterministic but biased: at volume, whichever
SKU sorts first absorbs every rounding error.

## Application order is part of the contract

A $100 order with "20% off" and "$15 off" pays $65 or $68 depending on which
applies first. Neither is wrong; leaving it undocumented is. Canonical order:

1. **scope** — ITEM → CATEGORY → ORDER → SHIPPING
2. **within scope** — PERCENT/BOGO → TIERED → AMOUNT
3. **ties** — priority ascending, then `promo_id`

Every effect computes against what the customer *still owes*, so stacked
percentages compound rather than summing past 100%.

## Measured

**Latency** — checkout calls this synchronously, so it has a budget (25 ms p99):

| active promos | p50 | p95 | p99 | vs budget |
|---|---|---|---|---|
| 10 | 0.23 ms | 0.35 ms | 0.73 ms | OK |
| 100 | 0.93 ms | 1.93 ms | 2.86 ms | OK |
| 250 | 5.54 ms | 8.70 ms | 10.22 ms | OK |
| 500 | 10.40 ms | 16.77 ms | 20.17 ms | OK |

Evaluation is O(promos × lines) with **no eligibility index** — every promo is
tested against every cart. So this is a measurement of *this implementation*, not
a claim about promotion engines, and the linear scaling says exactly where it
breaks.

**Budget caps under concurrency** — 16 threads, 4,000 attempts, cap 1,000:

| check-and-increment | granted | overspend |
|---|---|---|
| unsynchronised | 4,000 | **+3,000** |
| under lock | 1,000 | 0 |

Honest limit: the guard is a process-local mutex, correct only because this is
one process. The real fix is a conditional `UPDATE ... WHERE remaining > 0` — the
same mechanism SE-1 uses for stock.

**Explanation trace** — a promo that vanishes silently is the most common
promotions escalation there is, so "every promo is applied or has a reason" is an
asserted property, and `explain()` prints per-promo, per-line contributions.

## The other 80% — what is NOT here

- **No API and no persistence.** Promotions are in-memory dataclasses; there is
  no HTTP surface, no merchant CRUD, no database, no scheduled activation/expiry
  (the time window is a `days_of_week` field, not a real calendar).
- **No eligibility index or promo-set compilation/caching**, which is the stated
  fix for the latency curve above and is therefore untested.
- **Per-customer limits are modelled but not enforced** — `per_customer_limit`
  exists on `Promotion` and nothing reads it.
- **Budget-cap atomicity is demonstrated, not solved** (see above).
- **Tax is entirely absent**, and it is not a small omission: discount-then-tax
  vs tax-then-discount changes the total, and the allocation of an order-level
  discount across lines with *different tax rates* is its own correctness problem.
- **No currency handling** — single implicit currency, no FX, no per-currency
  rounding rules (some currencies have no minor unit at all, which would break
  the cent-based allocator).
- **No BOGO variant coverage.** The engine implements cheapest-within-group and
  documents the choice; there is no example-based test pinning the *policy*,
  because properties cannot see policy (BUGS_FOUND.md #5).
- **`best_of_resolution` is exponential** and capped at 12 eligible promos.

**Linkage to SE-1:** the line-item allocation here is what makes proportional
promo refunds computable on a partial return. That is the arithmetic SE-1's OMS
needs and the reason the allocation invariant is exact rather than approximate.
