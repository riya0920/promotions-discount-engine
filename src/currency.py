"""Currency: minor units, rounding rules, and the ones with no minor unit at all.

THE GAP, AND WHY THE README FLAGGED IT AS A BREAKING ONE
---------------------------------------------------------
"No currency handling -- single implicit currency, no FX, no per-currency
rounding rules (some currencies have no minor unit at all, which would break the
cent-based allocator)."

That last clause is the whole problem. The allocator's contract is that the parts
sum EXACTLY to the total, and it achieves that in the smallest indivisible unit.
For USD that unit is the cent, so `allocate` works on cents. For **JPY there is no
cent** -- the yen is the smallest unit -- and for **KWD there are one thousand
fils to the dinar**. An engine that hard-codes "two decimal places" will happily
issue a discount of 12.5 yen, which is not a quantity of money.

WHAT THIS MODULE IS AND IS NOT
------------------------------
It is a table of minor-unit exponents plus the conversions that let the existing
integer allocator keep working unchanged: everything is computed in MINOR UNITS,
and the only thing that varies is how many minor units make a major one.

It is NOT an FX system. There is no rate feed, no triangulation, no settlement,
and -- most importantly -- no opinion about WHEN a rate is struck, which is the
actually hard part of multi-currency commerce: the rate at cart, at
authorisation, at capture and at refund are four different numbers, and which one
a refund uses is a policy question that costs real money. That is named in the
remaining-work list rather than half-built.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Currency:
    code: str
    # ISO 4217 minor unit exponent: 2 for USD/EUR/GBP, 0 for JPY/KRW, 3 for KWD.
    exponent: int
    symbol: str = ""
    # Some currencies are conventionally rounded to a coarser unit than their
    # own minor unit. CHF prices are commonly rounded to 5 rappen at the till,
    # which is a ROUNDING rule and not a minor-unit change -- conflating the two
    # is how an engine ends up unable to represent a legal price.
    cash_rounding_step: int = 1


CURRENCIES = {
    "USD": Currency("USD", 2, "$"),
    "EUR": Currency("EUR", 2, "€"),
    "GBP": Currency("GBP", 2, "£"),
    "JPY": Currency("JPY", 0, "¥"),         # no minor unit at all
    "KRW": Currency("KRW", 0, "₩"),
    "KWD": Currency("KWD", 3, "KD"),             # 1000 fils to the dinar
    "CHF": Currency("CHF", 2, "CHF", cash_rounding_step=5),
}


def get(code: str) -> Currency:
    try:
        return CURRENCIES[code.upper()]
    except KeyError:
        # Defaulting to USD would silently mis-scale every amount in the cart.
        # An unknown currency is a configuration error, and the only safe
        # behaviour is to refuse.
        raise ValueError("unknown currency %r -- refusing to guess a minor unit"
                         % code)


def minor_units_per_major(code: str) -> int:
    return 10 ** get(code).exponent


def to_minor(major: float, code: str) -> int:
    """Major units (dollars, yen) -> minor units (cents, yen).

    Takes a float ONLY at this boundary, where a human or a feed hands over a
    price. Everything downstream is integer minor units, which is the entire
    argument of `money.py`.
    """
    scale = minor_units_per_major(code)
    return int(round(major * scale))


def format_minor(minor: int, code: str) -> str:
    c = get(code)
    if c.exponent == 0:
        return "%s%d" % (c.symbol, minor)
    scale = 10 ** c.exponent
    return "%s%d.%0*d" % (c.symbol, minor // scale, c.exponent, abs(minor) % scale)


def apply_cash_rounding(minor: int, code: str) -> int:
    """Round to the currency's cash step, half-up.

    Only the TOTAL is cash-rounded, never the individual lines. Rounding lines
    and then summing produces a total that does not match the sum of the printed
    lines, and a receipt whose lines do not add up is a support call regardless
    of which number is correct.
    """
    step = get(code).cash_rounding_step
    if step <= 1:
        return minor
    return int((minor + step // 2) // step * step)


def validate_amount(minor: int, code: str) -> None:
    """An amount must be a whole number of minor units. Enforced, not assumed."""
    if int(minor) != minor:
        raise ValueError("%r is not a whole number of %s minor units"
                         % (minor, code))


def convert(minor: int, frm: str, to: str, rate_major_per_major: float) -> int:
    """Convert between currencies at a caller-supplied rate.

    THE RATE IS AN ARGUMENT, DELIBERATELY. This module will not fetch one,
    because the interesting question is not where the number comes from but WHEN
    it is struck: the rate at cart, at authorisation, at capture and at refund
    are four different numbers, and a refund issued at today's rate on a purchase
    made at last month's is a loss nobody budgeted for. Making the caller supply
    it forces that decision to live somewhere a person owns.

    Conversion goes through major units because the exponents differ: 100 US
    cents at 150 JPY/USD is 150 yen, not 15,000.
    """
    src, dst = get(frm), get(to)
    major = minor / (10 ** src.exponent)
    return int(round(major * rate_major_per_major * (10 ** dst.exponent)))
