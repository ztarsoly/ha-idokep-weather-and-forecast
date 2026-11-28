"""
Rounding with halves away from zero (2.5 to 3, -2.5 to -3).

Python's round() takes a half to the even neighbour instead.
"""

from __future__ import annotations

import math


def round_to(value: float, digits: int) -> float:
    """Round to a number of decimals, a half away from zero."""
    scale = 10**digits
    # Cleared of binary noise first: 2.675 * 100 is 267.49999999999997
    scaled = round(abs(value) * scale, 6)
    return math.copysign(math.floor(scaled + 0.5) / scale, value)


def round_whole(value: float) -> int:
    """Round to a whole number, a half away from zero."""
    return int(round_to(value, 0))
