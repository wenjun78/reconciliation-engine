"""Exact integer money arithmetic.

All amounts are Python ``int`` (arbitrary precision).  Nothing in this module
ever touches ``float``; the spec requires exact results above 2**53.
"""

from __future__ import annotations

import re

_INT_TEXT = re.compile(r"^[+-]?[0-9]+$")


def parse_int_text(text: str) -> int:
    """Parse a signed base-10 integer string exactly.

    Deliberately stricter than ``int()``: no whitespace, underscores or
    exponent forms are accepted, so a v2 ``amount_minor_text`` can never be
    silently reinterpreted.
    """
    if not isinstance(text, str) or not _INT_TEXT.match(text):
        raise ValueError(f"not a base-10 integer string: {text!r}")
    return int(text)


def div_half_even(numerator: int, denominator: int) -> int:
    """Return numerator/denominator rounded to nearest, ties to even.

    Works for negative numerators symmetrically (round-half-even is an odd
    function, so floor-based evaluation on the non-negative remainder is
    exact).  ``denominator`` must be > 0.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    q, r = divmod(numerator, denominator)  # floor division, 0 <= r < den
    twice = r * 2
    if twice > denominator or (twice == denominator and (q & 1)):
        q += 1
    return q


def to_usd_micro(amount_minor: int, usd_micro_num: int, usd_micro_den: int) -> int:
    """Convert a signed minor-unit amount at an exact fraction rate.

    One entry is rounded independently; callers must never round a sum.
    """
    return div_half_even(amount_minor * usd_micro_num, usd_micro_den)
