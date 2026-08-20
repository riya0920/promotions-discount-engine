"""Money is integer cents. There are no floats anywhere in this engine.

A promotions engine that computes discounts in floating point will eventually
produce a cart whose line discounts do not sum to its order discount, and the
difference will surface downstream in a refund, months later, as a penny that
accounting cannot explain. Cents are integers; percentages are applied with an
explicit rounding rule; and the residual cent of any split is assigned by a
documented, deterministic method.
"""
from __future__ import annotations


def pct_of(amount_cents: int, pct_bp: int) -> int:
    """`pct_bp` basis points of `amount_cents`, rounded half-to-even.

    Half-to-even (banker's rounding) rather than half-up: applied across millions
    of lines, half-up is biased upward by half a cent per tie, and the party it
    is biased against is the merchant. Python's round() on an int quotient will
    not do this for us, so the tie case is written out.
    """
    num = amount_cents * pct_bp
    q, r = divmod(num, 10_000)
    half = 10_000 // 2
    if r > half or (r == half and q % 2 == 1):
        q += 1
    return q


def allocate(total_cents: int, weights: list[int]) -> list[int]:
    """Split `total_cents` across `weights` so the parts sum EXACTLY to the total.

    Largest-remainder (Hamilton) apportionment. The alternative -- give every
    residual cent to the first line -- is also deterministic but biased: at
    volume, line 1 of every cart absorbs the rounding, which shows up as a
    systematic discrepancy on whichever SKU merchandising happens to sort first.
    Largest-remainder spreads residuals to the lines with the strongest claim,
    and ties break on index so the result is still reproducible.

    Returns a list the same length as `weights` summing to exactly total_cents.
    """
    if not weights:
        return []
    w_sum = sum(weights)
    if w_sum <= 0:
        # nothing to weight by: put it all on the first slot rather than
        # silently dropping cents
        out = [0] * len(weights)
        out[0] = total_cents
        return out

    base, remainders = [], []
    for i, w in enumerate(weights):
        num = total_cents * w
        q, r = divmod(num, w_sum)
        base.append(q)
        remainders.append((r, -i))  # -i so lower index wins a tie
    short = total_cents - sum(base)
    for _, negi in sorted(remainders, reverse=True)[:short]:
        base[-negi] += 1
    assert sum(base) == total_cents
    return base


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return "%s$%d.%02d" % (sign, c // 100, c % 100)
