"""
The last downloaded forecast: saved to disk and followed while downloads fail.

The coordinator saves the forecast page's last successfully downloaded forecast
in one file per config entry under .storage/ (Home Assistant's Store helper).
It is used in two ways:

- while the page can't be downloaded, also after a restart, the forecast shown
  is the last one, followed hour by hour (forecast_as_of)
- the page lists only the coming hours, and in the evening it no longer
  shows today: the parser adds the missing hours and today to a successful
  download (what no earlier one had comes from the hours around the missing
  ones and from today's hours)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .api import (
    HourlyForecastParser,
    TimeUtils,
    WindStrengthMapper,
    current_hour_values,
    daily_item_from_hours,
    filled_hours,
    hour_start,
)
from .const import DOMAIN, HOURLY

STORAGE_VERSION = 1
# Delay before writing
SAVE_DELAY = 10

# The forecast page's data that is saved
SAVED_KEYS = ("hourly_forecast", "daily_forecast")
# Today's max/min: (data key, daily item key)
TODAY_KEYS = (
    ("temperature_max_today", "temperature"),
    ("temperature_min_today", "templow"),
)


def storage_key(entry_id: str) -> str:
    """Return the Store key (file name under .storage/) for a config entry."""
    return f"{DOMAIN}.{entry_id}"


def station_storage_key(entry_id: str) -> str:
    """Return the Store key of the station readings saved by 1.12.7-1.12.8."""
    return f"{DOMAIN}.{entry_id}.station"


def serialize(location: str, forecast: dict[str, Any]) -> dict[str, Any]:
    """Return what to save of a downloaded forecast page (all plain data)."""
    data = {key: forecast[key] for key in SAVED_KEYS if key in forecast}
    # Saved data is only restored for the same location
    return {"location": location, "sources": {HOURLY: {"data": data}}}


def restored_forecast(saved: dict[str, Any]) -> dict[str, Any]:
    """
    Return the saved hourly and daily forecast in the current form.

    Empty lists if the file has none or can't be read.
    """
    return {
        "hourly_forecast": [
            _with_wind_estimates(item)
            for item in _saved_items(saved, "hourly_forecast")
        ],
        "daily_forecast": [
            daily
            for item in _saved_items(saved, "daily_forecast")
            if (daily := _daily_item(item)) is not None
        ],
    }


def today_item(forecast: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Return the forecast's daily entry of today, if there is one."""
    for item in forecast.get("daily_forecast", []):
        if _day(item) == today:
            return item
    return None


def running_hour(forecast: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Return the forecast's hourly item whose hour contains now, if there is one."""
    for item in forecast.get("hourly_forecast", []):
        start = hour_start(item)
        if start is not None and start <= now < start + timedelta(hours=1):
            return item
    return None


def forecast_as_of(forecast: dict[str, Any], now: datetime) -> dict[str, Any]:
    """
    Return what a downloaded forecast says at now (an aware datetime).

    The hours from the one now running, the days from today, today's max/min,
    and the current wind, precipitation and storm value from the running hour.
    Alerts and warnings are not part of it. Missing hours are added (see
    filled_hours), and a missing today is made from its hours. Empty if the
    forecast has neither the hour now running nor a later one.
    """
    past: list[dict[str, Any]] = []
    coming: list[dict[str, Any]] = []
    for item in forecast.get("hourly_forecast", []):
        if (start := hour_start(item)) is not None:
            (coming if start + timedelta(hours=1) > now else past).append(item)
    if not coming:
        return {}
    hourly = filled_hours(coming, past, now)

    today = dt_util.as_local(now).date()
    result: dict[str, Any] = {
        "hourly_forecast": hourly,
        **current_hour_values(hourly[0]),
        "storm_expected": HourlyForecastParser.storm_expected(hourly, now),
    }
    daily = [
        item
        for item in forecast.get("daily_forecast", [])
        if (day := _day(item)) is not None and day >= today
    ]
    item = today_item(forecast, today)
    if item is None:
        item = daily_item_from_hours([*past, *hourly], today)
        if item is not None:
            daily.insert(0, item)
    if daily:
        result["daily_forecast"] = daily
    if item is not None:
        for key, value_key in TODAY_KEYS:
            if item.get(value_key) is not None:
                result[key] = item[value_key]
    return result


def _day(item: dict[str, Any]) -> date | None:
    """Return a daily item's date (None without a readable one)."""
    try:
        return date.fromisoformat(str(item["datetime"])[:10])
    except (KeyError, ValueError):
        return None


def _daily_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return a saved daily entry in the current form (None without a date).

    Earlier versions saved a date-only datetime and a precipitation
    probability (the page shows none per day): the date becomes local
    midnight, the probability goes.
    """
    if (day := _day(item)) is None:
        return None
    kept = {k: v for k, v in item.items() if k != "precipitation_probability"}
    return {**kept, "datetime": TimeUtils.day_start(day)}


def _saved_items(saved: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return a list of the saved forecast (empty if missing or unreadable)."""
    try:
        items = saved["sources"][HOURLY]["data"][key]
        return [item for item in items if isinstance(item, dict)]
    except (KeyError, TypeError):
        return []


def _with_wind_estimates(item: dict[str, Any]) -> dict[str, Any]:
    """
    Return an hourly item with its wind speed and gust from its wind strength.

    They are only estimates from the strength, so data saved by an earlier
    version may have other estimates, or none.
    """
    strength = item.get("wind_strength")
    if strength not in WindStrengthMapper.SPEEDS:
        return item
    return {
        **item,
        "native_wind_speed": WindStrengthMapper.SPEEDS[strength],
        "native_wind_gust_speed": WindStrengthMapper.GUSTS[strength],
    }
