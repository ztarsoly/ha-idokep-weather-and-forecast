"""DataUpdateCoordinator for idokep."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import IdokepApiClientCommunicationError
from .const import CURRENT, HOURLY
from .storage import SAVE_DELAY, STORAGE_VERSION, restore, serialize, storage_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from logging import Logger

    from homeassistant.core import HomeAssistant

    from .data import IdokepConfigEntry

# Key a successful fetch of each source must contain
REQUIRED_KEYS = {
    CURRENT: "temperature",
    HOURLY: "hourly_forecast",
}


def next_quarter_hour(now: datetime) -> datetime:
    """Return the next x:00, x:15, x:30 or x:45 after now."""
    # Budapest's UTC offset is whole hours, so UTC boundaries match local ones
    now = dt_util.as_utc(now)
    floored = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    return floored + timedelta(minutes=15)


def next_full_hour(now: datetime) -> datetime:
    """Return the next x:00 after now."""
    now = dt_util.as_utc(now)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def floor_to_minute(now: datetime) -> datetime:
    """Drop seconds so timer jitter can't push a due check past its minute."""
    return now.replace(second=0, microsecond=0)


@dataclass
class SourceSchedule:
    """When a data source is fetched next."""

    next_regular: Callable[[datetime], datetime]
    retry_interval: timedelta
    # None means due immediately (nothing fetched yet)
    next_due: datetime | None = None

    def is_due(self, now: datetime) -> bool:
        """Return True if the source should be fetched at now."""
        return self.next_due is None or now >= self.next_due

    def mark_success(self, now: datetime) -> None:
        """Schedule the next regular fetch."""
        self.next_due = self.next_regular(now)

    def mark_failure(self, now: datetime) -> None:
        """Schedule a retry."""
        self.next_due = now + self.retry_interval


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class IdokepDataUpdateCoordinator(DataUpdateCoordinator):
    """
    Class to manage fetching data from the API.

    Each idokep.hu page has its own schedule:
    - current conditions (/idojaras/): every quarter hour, retry every minute
    - forecast (/elorejelzes/: hourly and daily forecast, alerts, today's
      max/min): every full hour, retry every 10 minutes

    There is no update_interval; async_tick is called at every whole minute
    and refreshes only when a source used by an enabled entity is due.
    """

    config_entry: IdokepConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        name: str,
        config_entry: IdokepConfigEntry,
    ) -> None:
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name=name,
            update_interval=None,
            config_entry=config_entry,
        )
        self.config_entry = config_entry
        self.schedules: dict[str, SourceSchedule] = {
            CURRENT: SourceSchedule(next_quarter_hour, timedelta(minutes=1)),
            HOURLY: SourceSchedule(next_full_hour, timedelta(minutes=10)),
        }
        # Last successful result per source, kept while a source is failing
        self._source_data: dict[str, dict[str, Any]] = {
            source: {} for source in self.schedules
        }
        # When each source's data was fetched (saved along with it)
        self._fetched: dict[str, datetime] = {}
        # Saved copy of _source_data under .storage/ (set up by async_restore)
        self._store: Store | None = None

    async def async_restore(self) -> None:
        """
        Load the data saved before the last restart.

        Called before the first refresh, so the entry can start with the saved
        data even if idokep.hu is unreachable. Only still valid data is used
        (see storage.restore); pages are fetched on schedule as usual.
        """
        self._store = Store(
            self.hass, STORAGE_VERSION, storage_key(self.config_entry.entry_id)
        )
        saved = await self._store.async_load() or {}
        location = self.config_entry.data["location"]
        if saved.get("location", location) != location:
            # Saved before the location was changed in the options
            self.logger.debug("Saved data is for %s, not used", saved["location"])
            saved = {}
        now = dt_util.utcnow()
        for source, item in saved.get("sources", {}).items():
            if source not in self._source_data:
                continue
            data = restore(source, item, now)
            if not data:
                continue
            self._source_data[source] = data
            self._fetched[source] = datetime.fromisoformat(item["fetched"])
            self.logger.debug("Restored saved %s data", source)

        # Let the parser keep today's daily entry (and so today's max/min)
        # once the page drops today's column
        client = self.config_entry.runtime_data.client
        today_item = self._restored_today_item(dt_util.as_local(now).date())
        if today_item is not None:
            client.restore_today_forecast(today_item)
        # Likewise the current hour, which the page no longer lists
        if hourly := self._source_data[HOURLY].get("hourly_forecast"):
            client.restore_hourly_forecast(hourly)

    def _restored_today_item(self, today: date) -> dict[str, Any] | None:
        """Return today's daily entry from the restored forecast page data."""
        forecast = self._source_data[HOURLY]
        for item in forecast.get("daily_forecast", []):
            if item.get("datetime") == today.isoformat():
                return item
        # Data saved by an earlier version only has today's max/min
        if "temperature_max_today" in forecast or "temperature_min_today" in forecast:
            return {
                "datetime": today.isoformat(),
                "temperature": forecast.get("temperature_max_today"),
                "templow": forecast.get("temperature_min_today"),
                "condition": None,
                "precipitation": 0,
                "precipitation_probability": 0,
            }
        return None

    def _data_to_save(self) -> dict[str, Any]:
        """Return the data to write to disk."""
        return {
            # Saved data is only restored for the same location
            "location": self.config_entry.data["location"],
            "sources": {
                source: {
                    "fetched": self._fetched[source].isoformat(),
                    "data": serialize(self._source_data[source]),
                }
                for source in self._source_data
                if source in self._fetched and self._source_data[source]
            },
        }

    def needed_sources(self) -> set[str]:
        """
        Return the sources used by at least one enabled entity.

        Entities register the pages they read as their listener context.
        Disabled entities (also those of a disabled device) never listen, so
        their pages drop out; with no entity listening nothing is needed.
        """
        needed: set[str] = set()
        for sources in self.async_contexts():
            needed |= sources
        return needed

    async def async_tick(self, now: datetime) -> None:
        """Refresh if a needed source is due (called at every whole minute)."""
        # Honour the entry's "Enable polling for updates" system option, as
        # Home Assistant's own polling does
        if self.config_entry.pref_disable_polling:
            return
        now = floor_to_minute(now)
        if any(self.schedules[source].is_due(now) for source in self.needed_sources()):
            await self.async_refresh()

    def _fetchers(self) -> dict[str, Callable[[str], Awaitable[dict[str, Any]]]]:
        """Return the API call for each source."""
        client = self.config_entry.runtime_data.client
        return {
            CURRENT: client.async_get_current_weather,
            HOURLY: client.async_get_hourly_forecast,
        }

    async def _async_update_data(self) -> dict:
        """Fetch the sources that are due and merge with the last good data."""
        location = self.config_entry.data["location"]
        now = floor_to_minute(dt_util.utcnow())
        # The first refresh runs during setup, before any entity listens; it
        # fetches everything so the entry can start with data
        wanted = set(self.schedules) if self.data is None else self.needed_sources()
        due = [
            source
            for source, schedule in self.schedules.items()
            if source in wanted and schedule.is_due(now)
        ]
        fetchers = self._fetchers()
        results = await asyncio.gather(
            *(fetchers[source](location) for source in due),
            return_exceptions=True,
        )

        for source, result in zip(due, results, strict=True):
            schedule = self.schedules[source]
            if isinstance(result, IdokepApiClientCommunicationError):
                self.logger.info("Fetching %s data failed: %s", source, result)
                schedule.mark_failure(now)
            elif isinstance(result, BaseException):
                self.logger.error(
                    "Unexpected error parsing %s data",
                    source,
                    exc_info=result,
                )
                schedule.mark_failure(now)
            elif REQUIRED_KEYS[source] not in result:
                self.logger.warning("No %s data found for %s", source, location)
                schedule.mark_failure(now)
            else:
                self._source_data[source] = result
                self._fetched[source] = now
                schedule.mark_success(now)
                if self._store is not None:
                    self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

        data: dict[str, Any] = {}
        for source in (CURRENT, HOURLY):
            data.update(self._source_data[source])

        if not data:
            msg = f"No weather data found for location: {location}"
            raise UpdateFailed(msg)
        return data
