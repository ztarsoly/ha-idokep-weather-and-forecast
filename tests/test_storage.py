"""Tests for saving the forecast to disk and reading back the parts needed."""

from __future__ import annotations

import json
import zoneinfo
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.idokep_weather_and_forecast.const import HOURLY
from custom_components.idokep_weather_and_forecast.storage import (
    forecast_as_of,
    restored_forecast,
    serialize,
    storage_key,
    today_item,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

BUDAPEST = zoneinfo.ZoneInfo("Europe/Budapest")
TODAY = date(2026, 10, 29)

FORECAST_DATA = {
    "hourly_forecast": [
        {"datetime": "2026-10-29T20:00:00+01:00", "temperature": 9},
        {"datetime": "2026-10-29T21:00:00+01:00", "temperature": 8},
    ],
    "wind_speed": 20,
    "precipitation": 0.81,
    "alerts": [{"type": "wind", "level": "yellow"}],
    "daily_forecast": [
        {"datetime": "2026-10-29T00:00:00+01:00", "temperature": 16},
        {"datetime": "2026-10-30T00:00:00+01:00", "temperature": 14},
    ],
    "temperature_max_today": 16,
}


@pytest.fixture(autouse=True)
def budapest_time_zone() -> Iterator[None]:
    """Run tests with Home Assistant set to the Budapest time zone."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(BUDAPEST)
    yield
    dt_util.set_default_time_zone(original)


def saved(data: dict) -> dict:
    """Return a downloaded page as it is written to disk (through JSON)."""
    return json.loads(json.dumps(serialize("Budapest", data)))


def test_storage_key() -> None:
    """One file per config entry."""
    assert storage_key("abc123") == "idokep_weather_and_forecast.abc123"


def test_only_the_forecasts_are_saved() -> None:
    """Nothing but the parts the page drops later is written."""
    assert saved(FORECAST_DATA) == {
        "location": "Budapest",
        "sources": {
            HOURLY: {
                "data": {
                    "hourly_forecast": FORECAST_DATA["hourly_forecast"],
                    "daily_forecast": FORECAST_DATA["daily_forecast"],
                }
            }
        },
    }


def test_a_page_without_daily_forecast() -> None:
    """Only what the page had is saved."""
    data = saved({"hourly_forecast": []})["sources"][HOURLY]["data"]
    assert data == {"hourly_forecast": []}


def test_restored_forecast() -> None:
    """The hourly items and the daily entries come back."""
    forecast = restored_forecast(saved(FORECAST_DATA))
    assert forecast == {
        "hourly_forecast": FORECAST_DATA["hourly_forecast"],
        "daily_forecast": FORECAST_DATA["daily_forecast"],
    }
    assert today_item(forecast, TODAY) == FORECAST_DATA["daily_forecast"][0]


def test_no_entry_of_today() -> None:
    """Other days' entries are not today's."""
    assert today_item(FORECAST_DATA, date(2026, 11, 2)) is None


def test_old_daily_item_updated() -> None:
    """
    A daily item saved by an earlier version is brought up to date.

    Its date-only datetime becomes local midnight, its precipitation
    probability goes; one without a date is dropped.
    """
    data = {
        "daily_forecast": [
            {"datetime": "y"},
            {"datetime": "2026-10-29", "precipitation": 3, "precipitation_probability": 70},
        ]
    }
    assert restored_forecast(saved(data))["daily_forecast"] == [
        {"datetime": "2026-10-29T00:00:00+01:00", "precipitation": 3}
    ]


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"sources": None},
        {"sources": {HOURLY: {}}},
        {"sources": {HOURLY: {"data": {"hourly_forecast": None}}}},
        {"sources": {HOURLY: {"data": {"hourly_forecast": ["broken"]}}}},
    ],
)
def test_broken_saved_data_restores_nothing(item: dict) -> None:
    """A damaged file never breaks setup."""
    assert restored_forecast(item) == {"hourly_forecast": [], "daily_forecast": []}


def test_restore_recalculates_the_wind_estimates() -> None:
    """Saved estimates are replaced by the current ones from the wind strength."""
    data = {
        "hourly_forecast": [
            {"wind_strength": "gentle_breeze", "native_wind_speed": 16},
            {"wind_strength": "unknown_word", "native_wind_speed": 9},
        ]
    }

    first, second = restored_forecast(saved(data))["hourly_forecast"]

    assert first["native_wind_speed"] == 12
    assert first["native_wind_gust_speed"] == 21
    assert second == {"wind_strength": "unknown_word", "native_wind_speed": 9}


FOLLOWED = {
    "hourly_forecast": [
        {"temperature": 1},
        {"datetime": "broken"},
        {"datetime": "2026-10-29T19:00:00+01:00", "wind_bearing": 90},
        {
            "datetime": "2026-10-29T20:00:00+01:00",
            "wind_bearing": 180,
            "precipitation": 0.2,
        },
        {"datetime": "2026-10-29T21:00:00+01:00", "storm": True},
    ],
    "daily_forecast": [
        {"datetime": "2026-10-28T00:00:00+01:00", "temperature": 15},
        {"datetime": "2026-10-29T00:00:00+01:00", "temperature": 16, "templow": 6},
        {"datetime": "2026-10-30T00:00:00+01:00", "temperature": 14},
    ],
    "alerts": [{"type": "wind", "level": "yellow"}],
    "wind_bearing": 90,
}


def at(hour: int, minute: int = 0, day: int = 29) -> datetime:
    """Return a Budapest-local time in October 2026."""
    return datetime(2026, 10, day, hour, minute, tzinfo=BUDAPEST)


def test_forecast_as_of() -> None:
    """From the running hour and today on, without alerts."""
    assert forecast_as_of(FOLLOWED, at(20, 30)) == {
        "hourly_forecast": FOLLOWED["hourly_forecast"][3:],
        "daily_forecast": FOLLOWED["daily_forecast"][1:],
        "wind_bearing": 180,
        "precipitation": 0.2,
        "storm_expected": True,
        "temperature_max_today": 16,
        "temperature_min_today": 6,
    }


@pytest.mark.parametrize(
    "now",
    [at(18, 59), at(22), at(20, day=30), at(20) - timedelta(days=3)],
)
def test_forecast_as_of_without_the_running_hour(now: datetime) -> None:
    """A forecast that doesn't have the running hour gives nothing."""
    assert forecast_as_of(FOLLOWED, now) == {}


def test_forecast_as_of_without_a_forecast() -> None:
    """Before anything was downloaded or saved there is nothing."""
    assert forecast_as_of({}, at(20)) == {}


def test_forecast_as_of_makes_a_missing_today_from_its_hours() -> None:
    """All of today's hours count, also the past ones; it comes first."""
    forecast = {
        "hourly_forecast": [
            {"datetime": "2026-10-29T19:00:00+01:00", "temperature": 11},
            {"datetime": "2026-10-29T20:00:00+01:00", "temperature": 9},
            {"datetime": "2026-10-29T21:00:00+01:00", "temperature": 8},
            {"datetime": "2026-10-30T00:00:00+01:00", "temperature": 2},
        ],
        "daily_forecast": [FOLLOWED["daily_forecast"][2]],
    }

    result = forecast_as_of(forecast, at(20, 30))

    today, tomorrow = result["daily_forecast"]
    assert today["datetime"] == "2026-10-29T00:00:00+01:00"
    assert (today["temperature"], today["templow"]) == (11, 8)
    assert tomorrow == FOLLOWED["daily_forecast"][2]
    assert result["temperature_max_today"] == 11
    assert result["temperature_min_today"] == 8


def test_forecast_as_of_without_daily_forecast() -> None:
    """No daily entries or hourly temperatures: no daily forecast, no max/min."""
    forecast = {"hourly_forecast": FOLLOWED["hourly_forecast"]}
    result = forecast_as_of(forecast, at(20))
    assert "daily_forecast" not in result
    assert "temperature_max_today" not in result
