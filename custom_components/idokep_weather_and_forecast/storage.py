"""
Saving the last good data of each idokep.hu page to disk.

The coordinator keeps one file per config entry under .storage/ (Home
Assistant's Store helper). After a restart the saved data is restored, but
only what is still valid:

- forecasts: hours and days that have not passed yet
- today's max/min: only if saved today
- everything else (current temperature/condition, alerts, current wind):
  only if fetched within MAX_AGE, since its age cannot be checked otherwise
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .api import AlertData
from .const import CURRENT, DOMAIN, HOURLY

STORAGE_VERSION = 1
# Restored data older than this is only used where it can be checked by date
MAX_AGE = timedelta(hours=1)
# Delay before writing, so several page fetches end in one write
SAVE_DELAY = 10

# Values whose validity is a calendar day rather than an age
TODAY_KEYS = ("temperature_max_today", "temperature_min_today")


def storage_key(entry_id: str) -> str:
    """Return the Store key (file name under .storage/) for a config entry."""
    return f"{DOMAIN}.{entry_id}"


def serialize(data: dict[str, Any]) -> dict[str, Any]:
    """Return page data in a JSON-safe form (alerts become plain dicts)."""
    result = dict(data)
    if "alerts" in result:
        result["alerts"] = [
            asdict(alert) if isinstance(alert, AlertData) else alert
            for alert in result["alerts"]
        ]
    return result


def restore(source: str, saved: dict[str, Any], now: datetime) -> dict[str, Any]:
    """
    Return the still valid part of a page's saved data.

    saved is {"fetched": ISO time, "data": page data}; now is an aware
    datetime. An empty result means nothing worth restoring.
    """
    try:
        fetched = datetime.fromisoformat(saved["fetched"])
        data = dict(saved["data"])
    except (KeyError, TypeError, ValueError):
        return {}
    restorer = _RESTORERS.get(source)
    if restorer is None:
        return {}
    return restorer(data, fetched, now)


def _restore_current(
    data: dict[str, Any], fetched: datetime, now: datetime
) -> dict[str, Any]:
    """Restore current conditions: only if fresh."""
    return data if now - fetched <= MAX_AGE else {}


def _restore_forecast(
    data: dict[str, Any], fetched: datetime, now: datetime
) -> dict[str, Any]:
    """
    Restore the forecast page's data.

    Hours not yet over, days from today on, today's max/min if saved today;
    alerts and current wind only if fresh.
    """
    result: dict[str, Any] = {}
    today = dt_util.as_local(now).date()

    if hourly := _hours_not_over(data.get("hourly_forecast", []), now):
        result["hourly_forecast"] = hourly
    if daily := _days_from(data.get("daily_forecast", []), today):
        result["daily_forecast"] = daily

    if dt_util.as_local(fetched).date() == today:
        result.update({key: data[key] for key in TODAY_KEYS if key in data})

    if now - fetched <= MAX_AGE:
        for key in ("alerts_by_level", "wind_bearing", "wind_strength", "wind_speed"):
            if key in data:
                result[key] = data[key]
        if "alerts" in data:
            result["alerts"] = [AlertData(**alert) for alert in data["alerts"]]

    return result


def _hours_not_over(items: list[dict], now: datetime) -> list[dict]:
    """Return the hourly items whose hour has not ended yet."""
    result = []
    for item in items:
        try:
            start = datetime.fromisoformat(item["datetime"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is not None and start + timedelta(hours=1) > now:
            result.append(item)
    return result


def _days_from(items: list[dict], today: date) -> list[dict]:
    """Return the daily items dated today or later."""
    result = []
    for item in items:
        try:
            day = date.fromisoformat(item["datetime"])
        except (KeyError, TypeError, ValueError):
            continue
        if day >= today:
            result.append(item)
    return result


_RESTORERS = {
    CURRENT: _restore_current,
    HOURLY: _restore_forecast,
}
