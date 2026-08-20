# SE-2 — Promotions & Discount Engine

**Roughly 50% of the spec.** The differentiator the hiring-manager doc names —
property-based testing aimed at rule *interactions* — plus the four things the
first pass listed as missing and has now built: an eligibility index, atomic
budget caps in shared state, per-line tax, and scheduled activation. What is
still absent is named at the bottom.

```bash
python -m pytest tests -q   # 31 tests, ~2min (Hypothesis: 250-400 examples each)
python run_engine.py        # trace, latency, index benchmark, budget races, tax
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
| **indexed evaluation == unindexed evaluation** | ✅ |
| tax per line sums to tax total; total still ≥ 0 with tax | ✅ |
| adding a promo never raises the total | ✅ *only* for genuinely combinable promos |
| removing an item never raises what remains pays | ✅ *only* excluding BOGO + thresholds |

Both qualifications were forced by counterexamples, not chosen for convenience.

## The two original findings

**A promotion worth nothing made a cart $5.00 more expensive.** A 0%-off ITEM
promo sorts ahead of a 20%-off CATEGORY promo, claims the shared `seasonal`
stacking class, and locks the valuable one out. Resolution picks the winner by
**sort order, not by value.**

My first version of that property excluded only `EXCLUSIVE` promos, assuming
exclusivity was the only mutual-exclusion mechanism. Hypothesis found the same
bug through stacking classes instead, which generalises it: *any* mutual-exclusion
mechanism resolved by priority breaks catalogue monotonicity.

Resolved as a **policy choice, not an arithmetic defect**: priority-greedy stays
the default, and `best_of_resolution()` is implemented as the alternative with a
second property asserting monotonicity *does* hold under it.

**Removing an item raised the price of what remained** — two 1¢ units with
buy-1-get-1, delete the paid one, and the survivor stops being free. Here the
**property was wrong, not the engine**: any promotion qualifying on the *other*
items in the cart necessarily breaks monotonicity under removal. Telling those
two cases apart is the actual skill the suite tests for.

## Money is integer cents

No floats anywhere. Percentages apply in basis points with **half-to-even**
rounding (half-up is biased upward by half a cent per tie, at the merchant's
expense). Order-level discounts are **allocated down to line items** by
largest-remainder apportionment, so `Σ line discounts == order discount` exactly.

## Application order is part of the contract

A $100 order with "20% off" and "$15 off" pays $65 or $68 depending on which
applies first. Neither is wrong; leaving it undocumented is. Canonical order:
**scope** (ITEM → CATEGORY → ORDER → SHIPPING), then **kind** (PERCENT/BOGO →
TIERED → AMOUNT), then priority and `promo_id`. Every effect computes against
what the customer *still owes*, so stacked percentages compound rather than
summing past 100%.

---

# Second pass: the four gaps the first pass named

## The eligibility index — and what it actually buys

The first README said the fix for linear-in-catalogue-size evaluation was a
pre-compiled eligibility index, and didn't build it. Built now, and the honest
answer is more interesting than a speedup number:

| catalogue shape | promos | candidates | naive p99 | indexed p99 | speedup |
|---|---|---|---|---|---|
| broad-match | 500 | 439 | 31.3 ms | 28.4 ms | 1.10× |
| broad-match | 2,000 | 1,670 | 124.9 ms | 144.1 ms | **0.87×** |
| sku-targeted | 500 | 63 | 25.7 ms | 16.9 ms | 1.52× |
| sku-targeted | 2,000 | **237** | 47.9 ms | 21.9 ms | **2.19×** |

**Indexing converts the cost from catalogue SIZE to catalogue BREADTH**, and
whether that helps is a property of the merchant's promotions, not of the engine.
On a broad-match catalogue — order-level percentages, free shipping, first-order
offers, all *universal* by construction — the index prunes nothing and its
bookkeeping makes things marginally worse. On a SKU-targeted catalogue, which is
what a real retailer has because most promotions are vendor-funded or clearing
specific overstock, 2,000 promotions collapse to 237 candidates.

So the claim is **not** "indexing makes promo evaluation faster". Shipping this
without measuring the customer's actual catalogue composition would be shipping a
benchmark, not an improvement.

**A bug the equality check caught.** ORDER-scoped promotions were indexed by
their category field — but `is_eligible` never consults categories for ORDER
scope, so they are universal. The index hid an order-level promo from any cart
lacking that category: **39 wrong answers on 300 carts**. An index that changes
the answer is not an optimisation. Equality against the unindexed path is now a
property test over generated carts, and it reads 0 mismatches across 1,800 carts
× 6 catalogues.

Compile cost (5.95 ms at 2,000 promos) is paid when a merchant edits the
catalogue, not per checkout — which is why the compiled object is a separate
class rather than work done inside `evaluate()`.

## Budget caps, actually solved

The first pass guarded the race with a `threading.Lock` and said plainly that a
mutex is not the fix — two checkout pods don't share a Python lock, so a
1,000-redemption promotion issues 1,000 *per pod*. Now it's in shared state, the
same shape SE-1 uses for stock:

```sql
UPDATE promo_budget SET remaining = remaining - 1
 WHERE promo_id = ? AND remaining > 0
```

24 threads, 6,000 attempted redemptions, cap 1,000, **no application-level lock
anywhere**: exactly **1,000 granted, 0 overspend, no ledger drift**, 4,522
claims/s. There is no window between the check and the decrement because there is
no check — the precondition rides in the `WHERE` clause.

**Per-customer limits** need a different mechanism and get one: that check is a
`COUNT` over another table, so it cannot ride in the `UPDATE` and genuinely needs
`BEGIN IMMEDIATE`. 12 threads racing for one customer's single allowed redemption
across 600 attempts → **1 granted**. Two constraint shapes, two mechanisms,
rather than one lock hopefully covering both.

**The release path** matters more than it looks: a redemption claimed at
price-quote and abandoned at payment must go back, or every abandoned checkout
permanently burns one and a 1,000-redemption promotion silently becomes a
600-redemption one. Release is idempotent — a retry returns `False` rather than
double-refunding — and an append-only redemption ledger is drift-checked against
the counter (`cap - remaining` must always equal the row count).

**Honest limit:** SQLite serialises writers, so this proves the *algorithm* is
race-free without proving it scales. On Postgres the same statement takes a row
lock and redemptions of *different* promotions proceed in parallel; here they
queue. The conditional-UPDATE shape transfers; the throughput number does not.

## Tax, per line, post-discount

Listed as absent and "not a small omission". A cart of taxable apparel + **exempt
groceries** + taxable electronics, with one 20%-off-order promotion allocated
across all three:

| line | gross | discount | paid | tax bp | tax |
|---|---|---|---|---|---|
| TEE-BLUE | $49.98 | −$8.00 | $41.98 | 875 | $3.67 |
| MILK | $13.47 | −$2.16 | $11.31 | **0** | **$0.00** |
| HEADPHONES | $89.99 | −$14.40 | $75.59 | 875 | $6.61 |

A single cart-level rate would have charged $11.27 instead of $10.28 — it taxes
the exempt groceries. **There is no single correct cart rate when the lines
differ**, which is exactly why the order-level discount has to be *allocated to
lines* before tax is computable at all. That is the same allocation SE-1's
partial refunds stand on: get it wrong and you are wrong twice, in two systems,
months apart.

Discount-then-tax vs tax-then-discount is a real choice and the engine has made
it: tax applies to what the customer actually pays, because a *retailer* discount
reduces the taxable receipt. A *manufacturer* coupon generally does not — the
retailer is reimbursed — and that case is **not** modelled.

## Scheduled activation

Enforced in the engine per cart, not by a cron job flipping an `active` flag. A
flag has a moment of being wrong: the job runs late, or twice, or its pod
restarts, and a sale goes live early to whoever checks out then. Evaluating the
timestamp has no such window — and it lets the merchant simulator answer *"what
would this cart cost on Friday"*, which is the question they actually ask.
Callers that pass no clock get the old behaviour, so adding scheduling did not
silently change every existing evaluation.

---

## Also measured

**Latency vs the 25 ms checkout budget** (naive path): 0.73 ms p99 at 10 promos,
2.86 at 100, 20.17 at 500.

**Explanation trace** — a promo that vanishes silently is the most common
promotions escalation there is, so "every promo is applied or has a reason" is an
asserted property, and `explain()` prints per-promo, per-line contributions.

## The other ~50% — what is still NOT here

- **No API and no persistence for the catalogue.** Promotions are in-memory
  dataclasses; there is no HTTP surface and no merchant CRUD. (Redemptions *are*
  persisted — that is what `src/budget.py` is.)
- **No currency handling** — single implicit currency, no FX, no per-currency
  rounding rules (some currencies have no minor unit at all, which would break
  the cent-based allocator).
- **Manufacturer coupons are not modelled**, so the tax base is always the
  post-discount amount.
- **The index does not prune on segment, first-order or day-of-week**, only on
  product scope and spend thresholds — and those would help exactly the universal
  bucket it currently cannot touch.
- **No cache invalidation.** `CompiledCatalogue` is rebuilt wholesale; a real
  deployment needs incremental updates when one promotion changes.
- **No BOGO variant coverage.** The engine implements cheapest-within-group and
  documents the choice; there is no example-based test pinning the *policy*,
  because properties cannot see policy (BUGS_FOUND.md #5).
- **`best_of_resolution` is exponential** and capped at 12 eligible promos.
- **No tax jurisdiction model** — `tax_bp` is a per-line input, and deciding what
  it should be (nexus, destination vs origin sourcing, product taxability codes)
  is the actual hard part and is entirely absent.

**Linkage to SE-1:** the line-item allocation here is what makes proportional
promo refunds computable on a partial return — and now also what makes per-line
tax computable. Same algorithm, two consumers, which is why the allocation
invariant is exact rather than approximate.
