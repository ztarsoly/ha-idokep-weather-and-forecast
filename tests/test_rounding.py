"""Rounding with halves away from zero."""

from __future__ import annotations

import pytest

from custom_components.idokep_weather_and_forecast.rounding import (
    round_to,
    round_whole,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.5, 1),
        (1.5, 2),
        (2.5, 3),
        (-0.5, -1),
        (-2.5, -3),
        (2.4, 2),
        (-2.4, -2),
        (-0.4, 0),
        (0, 0),
        (7, 7),
        # A half but for the noise of binary numbers
        (11 * (15 / 22) - 1e-12, 8),
    ],
)
def test_round_whole(value: float, expected: int) -> None:
    """Halves go away from zero, on both sides of it."""
    result = round_whole(value)

    assert result == expected
    assert isinstance(result, int)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.125, 0.13),
        (-0.125, -0.13),
        # Stored as a little less than the half it is written as
        (2.675, 2.68),
        (-1.005, -1.01),
        ((0.01 + 0.02) / 2, 0.02),
        (10.124, 10.12),
        (-10.124, -10.12),
        (3, 3),
        (0.004, 0),
    ],
)
def test_round_to_two_decimals(value: float, expected: float) -> None:
    """The decimals are those of the number as it is written."""
    assert round_to(value, 2) == expected
