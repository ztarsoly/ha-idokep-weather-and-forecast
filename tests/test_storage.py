"""Tests for saving page data to disk and restoring what is still valid."""

from __future__ import annotations

import json
import zoneinfo
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.idokep_weather_and_forecast.api import AlertData
from custom_components.idokep_weather_and_forecast.const import CURRENT, HOURLY
from custom_components.idokep_weather_and_forecast.storage import (
    restore,
    serialize,
    storage_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

BUDAPEST = zoneinfo.ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 10, 29, 20, 30, tzinfo=BUDAPEST)

CURRENT_DATA = {"temperature": 13, "condition": "cloudy"}
FORECAST_DATA = {
    "hourly_forecast": [
        {"datetime": "2026-10-29T19:00:00+01:00", "temperature": 10},
        {"datetime": "2026-10-29T20:00:00+01:00", "temperature": 9},
        {"datetime": "2026-10-29T21:00:00+01:00", "temperature": 8},
    ],
    "wind_bearing": 12,
    "wind_strength": "moderate_breeze",
    "wind_speed": 24,
    "precipitation_probability": 25,
    "alerts": [AlertData(level="yellow", type="wind", description="Szél")],
    "alerts_by_level": {"yellow": [{"type": "wind"}], "orange": [], "red": []},
    "daily_forecast": [
        {"datetime": "2026-10-28", "temperature": 15},
        {"datetime": "2026-10-29", "temperature": 16},
        {"datetime": "2026-10-30", "temperature": 14},
    ],
    "temperature_max_today": 16,
    "temperature_min_today": 6,
}


@pytest.fixture(autouse=True)
def budapest_time_zone() -> Iterator[None]:
    """Run tests with Home Assistant set to the Budapest time zone."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(BUDAPEST)
    yield
    dt_util.set_default_time_zone(original)


def saved(data: dict, fetched: datetime) -> dict:
    """Return page data as it is written to disk (through JSON)."""
    return json.loads(
        json.dumps({"fetched": fetched.isoformat(), "data": serialize(data)})
    )


def test_storage_key() -> None:
    """One file per config entry."""
    assert storage_key("abc123") == "idokep_weather_and_forecast.abc123"


def test_serialize_makes_alerts_json_safe() -> None:
    """Alert objects are stored as plain dicts."""
    data = serialize(FORECAST_DATA)
    assert data["alerts"] == [
        {"level": "yellow", "type": "wind", "description": "Szél", "icon_url": None}
    ]
    json.dumps(data)  # must not raise
    # The live data is left untouched
    assert isinstance(FORECAST_DATA["alerts"][0], AlertData)


class TestRestoreCurrent:
    """Current conditions page."""

    def test_fresh_restores_everything(self) -> None:
        """Under an hour old: all values come back."""
        item = saved(CURRENT_DATA, NOW - timedelta(minutes=40))
        assert restore(CURRENT, item, NOW) == CURRENT_DATA

    def test_old_restores_nothing(self) -> None:
        """Older than an hour: the current reading is no longer valid."""
        item = saved(CURRENT_DATA, NOW - timedelta(hours=3))
        assert restore(CURRENT, item, NOW) == {}


class TestRestoreForecast:
    """Forecast page: hourly and daily forecast, alerts, wind, today's max/min."""

    def test_fresh_keeps_everything_still_valid(self) -> None:
        """19:00 is over at 20:30; 20:00 still runs; yesterday's day is dropped."""
        item = saved(FORECAST_DATA, NOW - timedelta(minutes=30))
        result = restore(HOURLY, item, NOW)
        assert [h["datetime"][11:16] for h in result["hourly_forecast"]] == [
            "20:00",
            "21:00",
        ]
        assert [d["datetime"] for d in result["daily_forecast"]] == [
            "2026-10-29",
            "2026-10-30",
        ]
        assert result["wind_bearing"] == 12
        assert result["wind_strength"] == "moderate_breeze"
        assert result["wind_speed"] == 24
        assert result["precipitation_probability"] == 25
        assert result["alerts"] == FORECAST_DATA["alerts"]
        assert isinstance(result["alerts"][0], AlertData)
        assert result["alerts_by_level"] == FORECAST_DATA["alerts_by_level"]
        assert result["temperature_max_today"] == 16
        assert result["temperature_min_today"] == 6

    def test_old_same_day_keeps_forecasts_and_todays_values(self) -> None:
        """Alerts and wind can't be checked by time, so old ones are dropped."""
        item = saved(FORECAST_DATA, NOW - timedelta(hours=3))
        assert restore(HOURLY, item, NOW) == {
            "hourly_forecast": FORECAST_DATA["hourly_forecast"][1:],
            "daily_forecast": FORECAST_DATA["daily_forecast"][1:],
            "temperature_max_today": 16,
            "temperature_min_today": 6,
        }

    def test_next_day_drops_todays_values(self) -> None:
        """Yesterday's max/min are not today's; past hours and days go too."""
        item = saved(FORECAST_DATA, NOW - timedelta(hours=3))
        tomorrow = NOW + timedelta(hours=5)  # 01:30 on the 30th
        assert restore(HOURLY, item, tomorrow) == {
            "daily_forecast": FORECAST_DATA["daily_forecast"][2:]
        }

    def test_all_past_restores_nothing(self) -> None:
        """A forecast that is completely over is not restored."""
        item = saved(FORECAST_DATA, NOW - timedelta(days=2))
        assert restore(HOURLY, item, NOW + timedelta(days=2)) == {}

    def test_invalid_daily_dates_dropped(self) -> None:
        """Daily entries without a real date are not restored."""
        data = {"daily_forecast": [{"datetime": "y"}, {"datetime": "2026-10-30"}]}
        assert restore(HOURLY, saved(data, NOW - timedelta(hours=3)), NOW) == {
            "daily_forecast": [{"datetime": "2026-10-30"}]
        }


@pytest.mark.parametrize(
    "item",
    [{}, {"fetched": "not a date", "data": {}}, {"fetched": NOW.isoformat()}],
)
def test_broken_saved_data_restores_nothing(item: dict) -> None:
    """A damaged file never breaks setup."""
    assert restore(CURRENT, item, NOW) == {}


def test_unknown_source_restores_nothing() -> None:
    """Data for a page that is no longer used (e.g. the old "daily") is ignored."""
    assert restore("daily", saved({"daily_forecast": []}, NOW), NOW) == {}
