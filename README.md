# SE-2 — Promotions & Discount Engine

**Complete against the spec.** Property-based testing of rule interactions,
exact-cent line allocation, an explanation trace, a compiled eligibility index
that now prunes the universal bucket, lock-free budget caps, per-line
post-discount tax, **multi-currency including the ones that break a cent-based
allocator**, **best-of resolution that is linear rather than exponential**,
**incremental index maintenance with a consistency invariant**, and a merchant
console with an audit trail.

```bash
python run_engine.py         # ~4min  the property suite and the original sections
python run_complete.py       # ~6s    currency, resolution, pruning, incremental edits
uvicorn serve:app --port 8015   #      the merchant console and /price
python -m pytest tests -q    # 67 tests
```

## Currency — and the one that breaks a cent-based allocator

The previous README named this as a **breaking** gap rather than a missing
feature: *"some currencies have no minor unit at all, which would break the
cent-based allocator."*

| code | exponent | minor per major | 1234567 minor |
|---|---|---|---|
| USD | 2 | 100 | $12345.67 |
| **JPY** | **0** | **1** | **¥1234567** |
| **KWD** | **3** | **1000** | KD1234.567 |
| CHF | 2 | 100 | CHF12345.67 (cash-rounds to 5 rappen) |

The allocator's contract is that the parts sum **exactly** to the total, in the
smallest *indivisible* unit. For USD that is the cent. For JPY there is no cent —
the yen **is** the smallest unit — and for KWD there are a thousand fils to the
dinar. An engine that hard-codes two decimal places will happily issue a discount
of 12.5 yen, which is not a quantity of money.

The allocator is unchanged; everything is computed in **minor units** and only the
exponent varies. A parametrised test asserts exact summation in USD, JPY, KWD and
CHF.

**CHF cash rounding is a rounding rule, not a minor-unit change**, and conflating
the two leaves an engine unable to represent a legal price. Only the *total* is
cash-rounded, never the lines: rounding lines and summing gives a total that does
not match the printed lines, and a receipt whose lines do not add up is a support
call whichever number is right.

**Not an FX system, and the reason is the interesting part.** `convert` takes the
rate as an *argument* because the hard question is not where the number comes from
but **when it is struck**: the rate at cart, at authorisation, at capture and at
refund are four different numbers, and a refund issued at today's rate on last
month's purchase is a loss nobody budgeted for. Forcing the caller to supply it
puts that decision somewhere a person owns.

## Best-of resolution without the exponential

A cap is not a solution. The previous `best_of_resolution` searched subsets and
stopped at 12 eligible promos, returning the best subset it happened to see — **a
wrong answer that looks like a right one.**

| promos | fast | exact | gap |
|---|---|---|---|
| 6 | 1.13 ms | 3.35 ms | **$0.00** |
| 8 | 0.64 ms | 3.67 ms | **$0.00** |
| 10 | 3.34 ms | 9.53 ms | **$0.00** |
| 12 | 1.08 ms | **182.85 ms** | **$0.00** |

**Why this is linear.** Compatibility here is not an arbitrary graph — it is a
**partition**. At most one promotion per stacking class may apply, and an
`EXCLUSIVE` promotion applies alone. Choosing at most one member per class is a
product of independent choices, so the best combination is the best member of each
class, compared against each exclusive on its own.

**Where it is exact and where it is not**, stated rather than assumed: exact when
promotions do not interact, approximate when they do — a percentage applied to an
already-discounted subtotal is set-dependent, so the best member of one class can
change once another applies. The brute force is kept and
`verify_against_bruteforce` exists for exactly that reason: a fast algorithm whose
approximation was never measured is one nobody should trust.

And the brute force now **refuses** rather than truncating. Raising an error on 13
promotions is worse ergonomics and better engineering than silently returning the
best of the first 12.

> A bug found on the way: promotions with **no** stack class shared one
> empty-string bucket, so two unrelated promotions became mutually exclusive
> because neither declared a class. Each now gets its own singleton class.

## Pruning the universal bucket

The previous index pruned on product scope and spend only, and said so: *"those
would help exactly the universal bucket it currently cannot touch."* That bucket
is where a broad-match catalogue's cost lives.

| catalogue | candidates before | candidates after | reduction |
|---|---|---|---|
| 200 | 155.6 | 120.5 | **22.5%** |
| 1,000 | 750.9 | 594.8 | **20.8%** |
| 3,000 | 2,267.6 | 1,764.0 | **22.2%** |

Segment, first-order and day-of-week are **cart** attributes rather than line
attributes, which is exactly why they work where a category index cannot: a
universal promotion has no product restriction to index on, but it may still be
restricted to VIPs on a Tuesday.

**Every one of these is a *necessary* condition of eligibility, never a sufficient
one.** The index may only remove promotions the engine would have rejected anyway;
anything stronger changes the answer, and an index that changes the answer is not
an optimisation. The equality property test against the unindexed path is what
enforces that, and it has caught this exact class of bug before — 39 wrong answers
on 300 carts, from indexing order-scoped promos by an unused category field.

The schedule window prunes **only when a clock is supplied**. Guessing "now"
inside an index is how a simulator that asks "what would this cart cost on Friday"
quietly returns Thursday's answer.

## Incremental edits, and the invariant that makes them safe

```
400 promotions loaded (with persistence)   881.6 ms
full index rebuild                          18.9 ms
200 edits, persistence + index            1677.1 ms   (8.385 ms each)
200 index updates alone                      4.2 ms   (0.0209 ms each)
index still matches a freshly built one:  True
```

**Read the last two rows together.** An edit costs 8.4 ms end to end and 0.02 ms
of that is the index — the rest is the SQLite commit. The incremental index is
**905× cheaper than a rebuild**; the *durability* is what an edit actually costs,
and no index strategy changes that. Timing them together would have attributed the
cost of a commit to the index and made the incremental path look barely worth
having.

A merchant editing **one** promotion should not rebuild a catalogue of two
thousand. The cost here is small — rebuilding is 19 ms — and the **shape** is the
problem: a real console edits continuously, and rebuilding on every save is how it
becomes unusable at scale.

**The consistency check is the load-bearing part.** An incrementally maintained
index that drifts from its source is worse than no index, because it is
confidently wrong rather than absent — and the symptom, a deleted promotion that
keeps firing, is very hard to reproduce. A test applies 120 random edits and
asserts after **every one** that the index matches a freshly built one.

The store owns both the table and the index and updates them in one call, because
any arrangement where a caller can write one without the other will eventually be
used by someone in a hurry. Empty index buckets are dropped on delete: leaving
them behind leaks one entry per SKU ever promoted, a slow leak that only appears
after months of merchandising.

## BOGO policy, pinned by example

`BUGS_FOUND.md` #5 records that **properties cannot see policy**: a property suite
can prove BOGO is monotone and self-consistent without ever pinning *which* unit
is free. Four example-based tests now do:

- the **cheapest** unit in the group is the free one;
- an incomplete buy quantity discounts nothing;
- the offer repeats once per complete group;
- the discount never exceeds the cart's own value.

That is the division of labour: properties find the interactions nobody thought
of, examples pin the decisions somebody made.

## The merchant console

`uvicorn serve:app --port 8015`

**Two endpoints, two very different latency budgets, and that is the design.**
`POST /price` runs inside a checkout, so it reads the compiled index and never
does work proportional to the catalogue. `PUT /promotions/{id}` is a merchant
editing a rule — it can afford to be slow, and it is the **only** path that
writes, so it is where the index is kept in step.

- **Every promotion that did not apply gets a reason** in the `/price` response,
  distinguishing "pruned by index" from an eligibility failure from "lost stacking
  resolution". A promo that vanishes silently is the single most common promotions
  escalation there is, and "it did not match" is not an answer a merchant can act
  on.
- **`/price` accepts `at`** as epoch seconds, so a merchant can ask *what would
  this cart cost on Friday*. Scheduling is evaluated per cart rather than by a cron
  job flipping a flag — a flag has a moment of being wrong; a timestamp does not.
- **Every edit is audited** with an actor and an action. "Who turned this on at
  4am" is otherwise unanswerable.
- A malformed promotion is **422**, not 500.

## What is deliberately not here

- **No tax jurisdiction model.** `tax_bp` is a per-line input, and deciding what
  it should be — nexus, destination vs origin sourcing, product taxability codes —
  is the actual hard part and is entirely absent.
- **No FX rates.** By choice, and the docstring argues why: the rate is the easy
  half, the timing is the hard half.
- **Manufacturer coupons are modelled in SE-1's tax layer, not here.** This engine
  still assumes every discount is retailer-funded when computing the tax base.
- **The catalogue store is JSON blobs keyed by id**, not a normalised schema. A
  real merchandising system needs eligibility as columns so a merchant can ask
  "which promotions target this category" without scanning — and that query is the
  whole reason a promotions console exists.
- **SQLite, not Postgres.** The conditional-`UPDATE` budget cap proves the
  algorithm is race-free without proving it scales: SQLite serialises writers, so
  redemptions of *different* promotions queue here and would proceed in parallel on
  Postgres. The shape transfers; the throughput number does not.
- **No bandit on boost value**, no per-session promo fatigue, no personalised
  offers.

**Linkage to SE-1:** the line-item allocation here is what makes proportional
promo refunds computable on a partial return, and it is what makes per-line tax
computable at all. Same algorithm, two consumers, which is why the allocation
invariant is exact rather than approximate.
