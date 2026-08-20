# BUGS_FOUND.md

What the property suite caught that an example-based suite would not have, with
the minimal cart Hypothesis shrank each one to. Two of the four were found by
generated counterexamples; the other two are recorded because they are the same
class of defect and it would be dishonest to imply the properties found
everything.

Each entry says what the property asserted, what the engine actually did, and
**what was decided** — because for two of these the correct resolution was to
change the property, not the code, and being able to tell those apart is the
whole skill.

---

## #1 — A worthless promotion crowds out a valuable one

**Property:** `test_adding_a_promotion_never_increases_the_total` — adding a
promotion to the catalogue must never make a cart more expensive.

**Status:** VIOLATION CONFIRMED. Pinned as `test_regression_stack_class_crowd_out`.

**Minimal case** (shrunk from a 5-line cart to 1 line):

```
cart:  1 line, SKU0, grocery, $4.17 x 6  =  $25.02

P_ITEM_0PCT   scope=ITEM      0% off   stack_class="seasonal"  priority=1
P_CAT_20PCT   scope=CATEGORY  20% off  stack_class="seasonal"  priority=1
```

| catalogue | total paid |
|---|---|
| `[P_CAT_20PCT]` | **$20.02** |
| `[P_ITEM_0PCT, P_CAT_20PCT]` | **$25.02** |

Adding a promotion made the cart **$5.00 more expensive.**

**Mechanism.** Resolution is priority-greedy in canonical order, and ITEM scope
sorts before CATEGORY scope. So the 0%-off promotion is considered first, is
eligible, is accepted, and claims the `seasonal` stacking class. The 20%-off
promotion then hits `stack_class_conflict:seasonal` and is rejected. The engine
picked the winner by **sort order, not by value.**

The first version of the property excluded only `EXCLUSIVE` promotions, on the
assumption that exclusivity was the only mutual-exclusion mechanism. Hypothesis
found the counterexample using stacking classes instead, which generalises the
finding: **any mutual-exclusion mechanism resolved by priority breaks catalogue
monotonicity.** Exclusivity was not special; it was just the one I had thought of.

**Decided:** priority-greedy stays the default, because merchant-configured
priority is what merchants mean when they set priorities, and a merchant is
entitled to say "this promotion wins even though it is worth less". But the
alternative is implemented as `best_of_resolution()` — choose the cheapest
compatible subset for the customer — and a second property asserts monotonicity
*does* hold under it. That demonstrates the violation is a **policy choice, not
an arithmetic defect**, which is the distinction that matters when a merchant
asks why their sale behaved that way.

`best_of_resolution` is exponential in the eligible-promo count and is not what
you would ship at 100+ active promotions without the candidate pruning listed as
unbuilt in the README.

---

## #2 — Removing an item raises the price of what remains

**Property:** `test_removing_an_item_never_raises_the_price_of_what_remains` —
deleting a line must not increase what the surviving lines pay.

**Status:** VIOLATION CONFIRMED, then reclassified as **correct behaviour**.
Pinned as `test_regression_bogo_dissolves_when_a_line_is_removed`.

**Minimal case:**

```
cart:  SKU0 grocery $0.01 x 1
       SKU0 grocery $0.01 x 1
promo: buy 1 get 1 free
```

Two units, one group, one free unit → merchandise paid $0.01. Delete the *paid*
unit, and the survivor no longer has anything to "buy", so it stops being free:
what it pays goes from **$0.00 to $0.01**.

**Decided: the property was wrong, not the engine.** Any promotion whose
qualification depends on the *other* items in the cart necessarily breaks
monotonicity under removal — that is what "buy 2 get 1" means, and a merchant
would be alarmed if it did not. The generator for this property now excludes
BOGO **and** threshold-bearing promotions, and both exclusions are documented as
findings rather than conveniences. With those two classes removed, the property
still earns its keep: it asserts that nothing *else* in the engine has acquired
a cart-wide dependency by accident.

The original docstring claimed "BOGO makes it non-trivial". That was wrong in an
instructive way: BOGO does not make it non-trivial, BOGO makes it **false**.

---

## #3 — Threshold promotions, same class of problem

**Status:** anticipated, confirmed by the same counterexample search, resolved by
scoping the property.

"Free shipping over $50" has the identical structure: remove a line, drop below
$50, and shipping reappears on the survivors. This is the case the hiring-manager
spec raises as a grill question ("cart hits $50 only WITH the free item counted —
in or out?"). The answer this engine gives is explicit and testable rather than
accidental:

- Eligibility thresholds are evaluated against `cart.subtotal`, the **pre-discount**
  merchandise total (`is_eligible` in `src/engine.py`).
- So a BOGO free unit **still counts toward** the free-shipping threshold, because
  the threshold never sees the discount.

That is a decision, not a fallout, and it is the one merchants usually want — but
it is written down here precisely because the opposite choice is defensible and a
platform that has not chosen will do both depending on evaluation order.

---

## #4 — Unsynchronised budget caps overspent by 300%

**Status:** VIOLATION CONFIRMED by the concurrency harness (`run_engine.py` §3),
not by the property suite.

16 threads, 4,000 attempted redemptions, cap of 1,000:

| check-and-increment | cap | granted | overspend |
|---|---|---|---|
| unsynchronised | 1,000 | **4,000** | **+3,000** |
| under lock | 1,000 | 1,000 | 0 |

Eligibility is *checked* and the counter is *incremented* in two separate steps,
so every thread reads a stale count and redeems. This is decrement-and-hope
inventory wearing a promotions hat, and the cost is margin nobody authorised.

**Honest limit:** the guarded version holds a process-local mutex, which is only
correct because this is one process. The real fix is making the check and the
increment one atomic operation in shared state — a conditional `UPDATE ... WHERE
remaining > 0` — which is exactly the mechanism SE-1 uses for stock. What is
demonstrated here is the failure and the shape of the fix, not the fix.

---

## #5 — BOGO gave away the wrong units (found by reading, not by properties)

Recorded because the properties **did not** catch it, and pretending otherwise
would misrepresent what property testing does.

The first implementation sorted units ascending and freed the globally cheapest
`n_groups × free` units. The industry-standard reading of "buy 2 get 1 free" is
to sort descending, group into threes, and free the cheapest unit *within each
group*. On a cart of six units at $10/$10/$8/$8/$6/$6 the two readings differ by
real money.

No invariant distinguishes them: both are non-negative, both reconcile to the
cent, both are deterministic. Properties check that the arithmetic is *coherent*,
not that the *policy* is the intended one. That still needs a specification and
an example-based test, and this repo has neither for the BOGO variant — it has a
documented choice in the code and this note.
