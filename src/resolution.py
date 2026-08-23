"""Best-of resolution that is not exponential, and an honest account of its bound.

THE GAP
-------
"`best_of_resolution` is exponential and capped at 12 eligible promos."

A cap is not a solution. It is a silent wrong answer at promo 13: the function
returns the best subset it happened to look at, with no signal that a better one
existed outside the window. That is worse than an error, because the number looks
fine.

WHAT MAKES THE PROBLEM TRACTABLE
--------------------------------
The exponential search is over all subsets, but the constraint structure is much
weaker than "any subset may be incompatible with any other". Compatibility here
is determined by TWO things only:

  * stacking class -- at most one promotion per class may apply;
  * exclusivity     -- an EXCLUSIVE promotion applies alone.

That is a partition, not an arbitrary graph. Choosing at most one member from
each class is a product of independent choices, so the best combination is found
by picking the best member OF EACH CLASS independently and then comparing that
against every single EXCLUSIVE promotion on its own. The search is

    O(n)  for the per-class maxima
  + O(k)  evaluations for k exclusive promotions
  + 1     evaluation of the combined stackable set

rather than O(2^n).

WHERE THIS IS EXACT AND WHERE IT IS NOT
----------------------------------------
It is EXACT when discounts are independent -- when the benefit of a promotion
does not depend on which others applied. It is NOT exact when they interact:
percentage promotions applied to an already-discounted subtotal are order- and
set-dependent, so the best member of class A can change once class B applies.

So this returns the exact answer under an assumption that is stated, and the
module also provides `verify_against_bruteforce` to check it on small inputs.
That check is the point: a fast algorithm whose approximation was never measured
is a fast algorithm nobody should trust.
"""
from __future__ import annotations

import itertools

from .engine import evaluate, is_eligible, resolve_stacking, sort_key
from .model import Cart, Evaluation, Promotion, Stacking


def _classes(promos: list[Promotion]) -> dict[str, list[Promotion]]:
    out: dict[str, list[Promotion]] = {}
    for p in promos:
        # A promotion with no stack class conflicts with nothing, so each gets
        # its own singleton class rather than sharing an empty-string bucket --
        # sharing one was the bug that made unrelated promotions block each other.
        key = p.stack_class or ("__solo__" + p.promo_id)
        out.setdefault(key, []).append(p)
    return out


def best_of_greedy(cart: Cart, promos: list[Promotion],
                   redemptions: dict[str, int] | None = None,
                   price_floor_cents: int = 0) -> Evaluation:
    """Per-class maxima plus each exclusive alone. Linear in the promo count."""
    eligible = [p for p in promos if is_eligible(p, cart, redemptions)[0]]
    eligible.sort(key=sort_key)

    exclusives = [p for p in eligible if p.stacking == Stacking.EXCLUSIVE]
    stackables = [p for p in eligible if p.stacking != Stacking.EXCLUSIVE]

    # Best single member of each stacking class, measured by what it saves ALONE.
    chosen = []
    for _cls, members in _classes(stackables).items():
        best_m, best_paid = None, None
        for m in members:
            ev = evaluate(cart, [m], redemptions, price_floor_cents)
            if best_paid is None or ev.total_paid < best_paid:
                best_m, best_paid = m, ev.total_paid
        if best_m is not None:
            chosen.append(best_m)

    candidates = []
    if chosen:
        acc, _ = resolve_stacking(chosen, cart, redemptions)
        candidates.append(evaluate(cart, acc, redemptions, price_floor_cents))
    for x in exclusives:
        candidates.append(evaluate(cart, [x], redemptions, price_floor_cents))
    candidates.append(evaluate(cart, [], redemptions, price_floor_cents))
    return min(candidates, key=lambda e: e.total_paid)


def best_of_bruteforce(cart: Cart, promos: list[Promotion],
                       redemptions: dict[str, int] | None = None,
                       price_floor_cents: int = 0,
                       max_promos: int = 12) -> Evaluation:
    """The exponential reference. Kept ONLY to check the fast one against.

    `max_promos` raises ValueError rather than silently truncating. The previous
    version capped the search and returned the best subset it happened to see,
    which is a wrong answer that looks like a right one -- the failure mode this
    whole module exists to remove.
    """
    eligible = [p for p in promos if is_eligible(p, cart, redemptions)[0]]
    if len(eligible) > max_promos:
        raise ValueError(
            "brute force refuses %d eligible promos (limit %d): it is exponential "
            "and truncating the search returns a wrong answer that looks right"
            % (len(eligible), max_promos))
    eligible.sort(key=sort_key)
    best = evaluate(cart, [], redemptions, price_floor_cents)
    for r in range(1, len(eligible) + 1):
        for subset in itertools.combinations(eligible, r):
            acc, _ = resolve_stacking(list(subset), cart, redemptions)
            if len(acc) != len(subset):
                continue
            ev = evaluate(cart, list(subset), redemptions, price_floor_cents)
            if ev.total_paid < best.total_paid:
                best = ev
    return best


def verify_against_bruteforce(cart: Cart, promos: list[Promotion],
                              redemptions: dict[str, int] | None = None,
                              price_floor_cents: int = 0) -> dict:
    """Run both and report the gap. The gap is what makes the fast one credible."""
    fast = best_of_greedy(cart, promos, redemptions, price_floor_cents)
    exact = best_of_bruteforce(cart, promos, redemptions, price_floor_cents)
    return dict(fast_total=fast.total_paid, exact_total=exact.total_paid,
                gap_cents=fast.total_paid - exact.total_paid,
                optimal=fast.total_paid == exact.total_paid)
