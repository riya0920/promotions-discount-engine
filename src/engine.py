"""Deterministic promotion evaluation with an explanation trace.

APPLICATION ORDER IS PART OF THE CONTRACT, because order changes the answer.
A $100 order with "20% off order" and "$15 off order" pays $65 if the percentage
applies first and $68 if the amount does. Neither is wrong; leaving it
undocumented is. The canonical order in this engine is:

    1. scope        ITEM  ->  CATEGORY  ->  ORDER  ->  SHIPPING
    2. within scope PERCENT_OFF / BOGO  ->  TIERED_SPEND  ->  AMOUNT_OFF
    3. ties         priority ascending, then promo_id ascending

Rationale for (1): narrow scopes first, so an order-level percentage applies to
what the customer is actually going to pay for the merchandise rather than to a
pre-discount figure. Rationale for (2): percentages are computed against the
larger base, which is the customer-favourable reading and the one merchants
describe in copy ("20% off, plus $15 off"). Rationale for (3): a total order
that depends on dictionary iteration order is not a total order.

Order-level discounts are ALLOCATED DOWN TO LINE ITEMS. That is not decoration:
without it, a partial return cannot compute what to refund for the returned line,
and the refund arithmetic in an order-management system has nothing to stand on.
The invariant `sum(line discounts) == order discount` is exact, to the cent.
"""
from __future__ import annotations

from .model import (Cart, EffectKind, Evaluation, Line, LineResult, Promotion,
                    Scope, Stacking)
from .money import allocate, pct_of

SCOPE_RANK = {Scope.ITEM: 0, Scope.CATEGORY: 1, Scope.ORDER: 2, Scope.SHIPPING: 3}
KIND_RANK = {
    EffectKind.PERCENT_OFF: 0,
    EffectKind.BOGO: 0,
    EffectKind.TIERED_SPEND: 1,
    EffectKind.FREE_SHIPPING: 1,
    EffectKind.AMOUNT_OFF: 2,
}


def sort_key(p: Promotion):
    return (SCOPE_RANK[p.scope], p.priority, KIND_RANK[p.kind], p.promo_id)


# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------
def matching_lines(promo: Promotion, cart: Cart) -> list[int]:
    """Indices of lines this promo's scope touches."""
    el = promo.eligibility
    idx = []
    for i, ln in enumerate(cart.lines):
        if el.skus and ln.sku not in el.skus:
            continue
        if el.categories and ln.category not in el.categories:
            continue
        idx.append(i)
    return idx


def is_eligible(promo: Promotion, cart: Cart, redemptions: dict[str, int] | None = None,
                customer_id: str | None = None,
                per_customer: dict | None = None,
                now: float | None = None) -> tuple[bool, str]:
    el = promo.eligibility
    if now is not None:
        if el.starts_at is not None and now < el.starts_at:
            return False, "not_yet_active"
        if el.ends_at is not None and now > el.ends_at:
            return False, "expired"
    if el.first_order_only and not cart.is_first_order:
        return False, "not_first_order"
    if el.segments and cart.customer_segment not in el.segments:
        return False, "segment_mismatch"
    if el.days_of_week and cart.day_of_week not in el.days_of_week:
        return False, "outside_time_window"
    if cart.subtotal < el.min_subtotal_cents:
        return False, "below_min_subtotal"
    if sum(ln.qty for ln in cart.lines) < el.min_qty:
        return False, "below_min_qty"
    if promo.scope in (Scope.ITEM, Scope.CATEGORY) and not matching_lines(promo, cart):
        return False, "no_matching_lines"
    if promo.kind == EffectKind.TIERED_SPEND and cart.subtotal < promo.tier_threshold_cents:
        return False, "below_tier_threshold"
    if redemptions is not None and promo.max_redemptions is not None:
        if redemptions.get(promo.promo_id, 0) >= promo.max_redemptions:
            return False, "budget_exhausted"
    # PER-CUSTOMER LIMIT. The field existed on Promotion from the start and
    # nothing read it -- a modelled-but-unenforced rule, which is worse than an
    # absent one because it reads as implemented.
    if (promo.per_customer_limit is not None and per_customer is not None
            and customer_id is not None):
        used = per_customer.get((promo.promo_id, customer_id), 0)
        if used >= promo.per_customer_limit:
            return False, "per_customer_limit_reached"
    return True, ""


def resolve_stacking(promos: list[Promotion], cart: Cart,
                     redemptions: dict[str, int] | None = None,
                     customer_id: str | None = None,
                     per_customer: dict | None = None,
                     now: float | None = None):
    """Greedy in canonical order: exclusivity and stack classes are honoured as
    the merchant configured them, first-come by priority.

    NOTE, and it is a real one: this is priority-greedy, not best-for-customer.
    A high-priority EXCLUSIVE promo can block two stackable promos that together
    would have been worth more, so adding a promotion to the catalogue can make a
    cart MORE expensive. The property suite found exactly that (see
    BUGS_FOUND.md #1). It is left as the default because it is what merchant
    priority configuration means; `best_of_resolution` is the alternative.
    """
    accepted, rejected = [], []
    chosen_exclusive = False
    used_classes: set[str] = set()

    for p in sorted(promos, key=sort_key):
        ok, why = is_eligible(p, cart, redemptions, customer_id, per_customer, now)
        if not ok:
            rejected.append((p.promo_id, why))
            continue
        if chosen_exclusive:
            rejected.append((p.promo_id, "blocked_by_exclusive"))
            continue
        if p.stacking == Stacking.EXCLUSIVE and accepted:
            rejected.append((p.promo_id, "exclusive_cannot_join_existing"))
            continue
        if p.stack_class and p.stack_class in used_classes:
            rejected.append((p.promo_id, "stack_class_conflict:" + p.stack_class))
            continue
        accepted.append(p)
        if p.stack_class:
            used_classes.add(p.stack_class)
        if p.stacking == Stacking.EXCLUSIVE:
            chosen_exclusive = True
    return accepted, rejected


# --------------------------------------------------------------------------
# effects
# --------------------------------------------------------------------------
def _bogo_discount(promo: Promotion, cart: Cart, idxs: list[int],
                   remaining: list[int]) -> list[int]:
    """Buy N get M free, taken on the CHEAPEST qualifying units.

    Units are expanded so a line with qty 3 can have one unit free and two paid;
    a promo engine that can only discount whole lines cannot express BOGO.
    """
    buy, free = promo.bogo
    if buy <= 0 or free <= 0:
        return [0] * len(cart.lines)
    units = []
    for i in idxs:
        ln = cart.lines[i]
        if ln.qty <= 0 or ln.unit_price_cents <= 0:
            continue
        units.extend([(ln.unit_price_cents, i)] * ln.qty)
    if not units:
        return [0] * len(cart.lines)

    # Sort DESCENDING and group; the free units are the cheapest WITHIN each
    # group. This is the standard merchant reading of "buy 2 get 1 free": three
    # units at $10/$8/$6 give away the $6 one, not the $10 one. Taking the
    # globally cheapest units instead is the customer-favourable variant and a
    # different promotion -- the choice has to be made explicitly, because with
    # mixed prices the two differ by real money.
    units.sort(key=lambda t: (-t[0], t[1]))
    group = buy + free
    n_groups = len(units) // group
    out = [0] * len(cart.lines)
    for g in range(n_groups):
        for k in range(free):
            price, li = units[g * group + buy + k]
            out[li] += price
    # do not discount more than the line still owes
    for i in range(len(out)):
        out[i] = min(out[i], remaining[i])
    return out


def apply_promo(promo: Promotion, cart: Cart, remaining: list[int],
                shipping_remaining: int) -> tuple[list[int], int, dict]:
    """Return (per-line discount, shipping discount, trace fragment).

    Every effect is computed against what the customer still owes, never against
    the original price, so two stacked percentages compound rather than sum past
    100%.
    """
    idxs = matching_lines(promo, cart) if promo.scope in (Scope.ITEM, Scope.CATEGORY) \
        else list(range(len(cart.lines)))
    per_line = [0] * len(cart.lines)
    ship_disc = 0
    detail = {}

    if promo.kind == EffectKind.PERCENT_OFF:
        for i in idxs:
            per_line[i] = pct_of(remaining[i], promo.percent_bp)
        detail["basis"] = "percent_of_remaining"

    elif promo.kind == EffectKind.BOGO:
        per_line = _bogo_discount(promo, cart, idxs, remaining)
        detail["basis"] = "cheapest_units_free"

    elif promo.kind in (EffectKind.AMOUNT_OFF, EffectKind.TIERED_SPEND):
        pool = sum(remaining[i] for i in idxs)
        want = min(promo.amount_cents, pool)
        # ALLOCATION: an order-level amount must land on the lines, exactly.
        parts = allocate(want, [remaining[i] for i in idxs])
        for i, part in zip(idxs, parts):
            per_line[i] = part
        detail["basis"] = "amount_allocated_largest_remainder"
        detail["requested_cents"] = promo.amount_cents
        detail["capped_to_cents"] = want

    elif promo.kind == EffectKind.FREE_SHIPPING:
        ship_disc = shipping_remaining
        detail["basis"] = "shipping_zeroed"

    # never discount a line below zero
    for i in range(len(per_line)):
        per_line[i] = max(0, min(per_line[i], remaining[i]))

    frag = dict(promo_id=promo.promo_id, scope=promo.scope.value,
                kind=promo.kind.value, priority=promo.priority,
                line_discounts=list(per_line), shipping_discount=ship_disc,
                total=sum(per_line) + ship_disc, **detail)
    return per_line, ship_disc, frag


def evaluate(cart: Cart, promos: list[Promotion],
             redemptions: dict[str, int] | None = None,
             price_floor_cents: int = 0,
             customer_id: str | None = None,
             per_customer: dict | None = None,
             now: float | None = None) -> Evaluation:
    """The whole pipeline: eligible -> resolve -> apply in canonical order -> tax."""
    accepted, rejected = resolve_stacking(list(promos), cart, redemptions,
                                          customer_id, per_customer, now)

    remaining = [ln.subtotal for ln in cart.lines]
    floor = [price_floor_cents * ln.qty for ln in cart.lines]
    shipping_remaining = cart.shipping_cents
    discounts = [0] * len(cart.lines)
    trace = []

    for p in sorted(accepted, key=sort_key):
        headroom = [max(0, remaining[i] - floor[i]) for i in range(len(remaining))]
        per_line, ship_disc, frag = apply_promo(p, cart, headroom, shipping_remaining)
        for i, d in enumerate(per_line):
            discounts[i] += d
            remaining[i] -= d
        shipping_remaining -= ship_disc
        trace.append(frag)

    lines = [LineResult(ln, discounts[i]) for i, ln in enumerate(cart.lines)]

    # TAX, per line, on the POST-DISCOUNT amount. Computed last and per line
    # because the rate differs by line: a cart with taxable electronics and
    # exempt groceries has no single rate, and an order-level discount allocated
    # across those lines changes the tax by a different amount on each one.
    # This is exactly why se1's refund math needs the per-line allocation to be
    # exact rather than approximate.
    tax_by_line = tuple(pct_of(lr.paid, lr.line.tax_bp) for lr in lines)
    return Evaluation(lines=lines, shipping_paid_cents=shipping_remaining,
                      trace=trace, applied=[p.promo_id for p in accepted],
                      rejected=rejected, tax_cents=sum(tax_by_line),
                      tax_by_line=tax_by_line)


def best_of_resolution(cart: Cart, promos: list[Promotion],
                       redemptions: dict[str, int] | None = None,
                       price_floor_cents: int = 0,
                       max_subsets: int = 4096) -> Evaluation:
    """Alternative resolution: choose the eligible, mutually-compatible subset
    that costs the customer least.

    This is the fix for the monotonicity violation the property suite found. It
    is exponential in the number of eligible promos, which is why it is not the
    default -- at 100+ active promotions it needs the candidate pruning the
    README lists as unbuilt. It exists here to demonstrate that the violation is
    a resolution-policy choice rather than an arithmetic bug.
    """
    import itertools

    eligible = [p for p in promos if is_eligible(p, cart, redemptions)[0]]
    eligible.sort(key=sort_key)
    best, best_total = None, None
    n = min(len(eligible), 12)
    combos = 0
    for r in range(len(eligible[:n]) + 1):
        for subset in itertools.combinations(eligible[:n], r):
            combos += 1
            if combos > max_subsets:
                break
            acc, _ = resolve_stacking(list(subset), cart, redemptions)
            if len(acc) != len(subset):
                continue  # subset is not internally compatible
            ev = evaluate(cart, list(subset), redemptions, price_floor_cents)
            if best_total is None or ev.total_paid < best_total:
                best, best_total = ev, ev.total_paid
    return best if best is not None else evaluate(cart, [], redemptions, price_floor_cents)


def explain(ev: Evaluation, cart: Cart) -> str:
    """The CS-agent / merchant-debugging surface.

    Every real promotions platform grows one of these, because the first
    question after "why is this cart $47.30" is unanswerable without it.
    """
    from .money import fmt
    out = ["cart subtotal        %10s" % fmt(cart.subtotal),
           "shipping             %10s" % fmt(cart.shipping_cents), ""]
    out.append("promotions considered:")
    for pid in ev.applied:
        out.append("  APPLIED  %s" % pid)
    for pid, why in ev.rejected:
        out.append("  rejected %-24s %s" % (pid, why))
    out.append("")
    out.append("application trace (canonical order):")
    for fr in ev.trace:
        out.append("  %-14s %-9s %-14s -> %s" % (
            fr["promo_id"], fr["scope"], fr["kind"], fmt(fr["total"])))
        for i, d in enumerate(fr["line_discounts"]):
            if d:
                out.append("       line %d %-14s %s" % (i, cart.lines[i].sku, fmt(-d)))
        if fr["shipping_discount"]:
            out.append("       shipping        %s" % fmt(-fr["shipping_discount"]))
    out.append("")
    for i, lr in enumerate(ev.lines):
        out.append("line %d %-14s qty %-3d %10s - %-10s = %10s" % (
            i, lr.line.sku, lr.line.qty, fmt(lr.line.subtotal),
            fmt(lr.discount_cents), fmt(lr.paid)))
    out.append("shipping paid        %10s" % fmt(ev.shipping_paid_cents))
    out.append("TOTAL                %10s" % fmt(ev.total_paid))
    return "\n".join(out)
