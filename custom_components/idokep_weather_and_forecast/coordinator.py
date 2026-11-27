"""The coordinators: the location's pages, the radar and the stations."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import IdokepApiClientCommunicationError, IdokepApiClientError
from .calculations import apparent_temperature
from .const import (
    CURRENT,
    DEFAULT_MAX_AGE,
    HOURLY,
    HUMIDITY_MAP,
    MERGE_PRIORITY,
    PRESSURE_MAP,
    UV_MAP,
)
from .failures import FailureLog
from .radar import FRAME_COUNT, NEWEST_INDEX, RadarImage, build_gif, fetch_frame
from .station import (
    StationData,
    StationReadings,
    fetch_station,
    latest_readings,
    merge_readings,
)
from .storage import (
    SAVE_DELAY,
    STORAGE_VERSION,
    forecast_as_of,
    restored_forecast,
    running_hour,
    serialize,
    storage_key,
    today_item,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from logging import Logger

    import aiohttp
    from homeassistant.core import HomeAssistant

    from .data import IdokepConfigEntry, RadarConfigEntry
    from .failures import FailureRecords

# Key a successful fetch of each source must contain
REQUIRED_KEYS = {
    CURRENT: "temperature",
    HOURLY: "hourly_forecast",
    HUMIDITY_MAP: "humidity",
    PRESSURE_MAP: "pressure",
    UV_MAP: "uv_index",
}


# The radar is checked on every 5th minute; in between only while waiting for
# a frame (the timer fires every minute, at second 59)
RADAR_CHECK_MINUTES = 5
# The stations are fetched on every 5th minute at second 59 (x:00:59,
# x:05:59, ...); after a failure, again every minute until it succeeds
STATION_CHECK_MINUTES = 5

# The entities reading each page, by the end of their unique ID. Needed at
# startup, before the entities listen, and for the sensors' listener contexts.
SOURCE_ENTITIES = {
    CURRENT: (
        "forecast",  # the weather entity
        "temperature",
        "condition",
        "cloud_coverage",
        "apparent_temperature",
        "storm_expected_1h",
    ),
    HOURLY: (
        "forecast",
        "apparent_temperature",
        "temperature_max_today",
        "temperature_min_today",
        "wind_bearing",
        "wind_direction",
        "wind_gust_speed",
        "wind_strength",
        "wind_speed",
        "precipitation",
        "precipitation_probability",
        "alert_level",
        "warning_level",
        "storm_expected_1h",
        "snow_today",
        "snow_tomorrow",
    ),
    HUMIDITY_MAP: ("humidity", "dew_point", "apparent_temperature"),
    PRESSURE_MAP: ("pressure",),
    UV_MAP: ("uv_index",),
}


# After a restart or reload every source that fails is retried every minute
# until it first works
STARTUP_RETRY_INTERVAL = timedelta(minutes=1)


def next_ten_minutes(now: datetime) -> datetime:
    """Return the next x:00, x:10, x:20, x:30, x:40 or x:50 after now."""
    # Budapest's UTC offset is whole hours, so UTC boundaries match local ones
    now = dt_util.as_utc(now)
    floored = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    return floored + timedelta(minutes=10)


def next_full_hour(now: datetime) -> datetime:
    """Return the next x:00 after now."""
    now = dt_util.as_utc(now)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def floor_to_minute(now: datetime) -> datetime:
    """Drop seconds so timer jitter can't push a due check past its minute."""
    return now.replace(second=0, microsecond=0)


# What the log calls each source
SOURCE_NAMES = {
    CURRENT: "current weather",
    HOURLY: "forecast",
    HUMIDITY_MAP: "humidity map",
    PRESSURE_MAP: "pressure map",
    UV_MAP: "UV map",
}


@dataclass
class SourceSchedule:
    """When a data source is fetched next."""

    next_regular: Callable[[datetime], datetime]
    retry_interval: timedelta
    # None means due immediately (nothing fetched yet)
    next_due: datetime | None = None
    # False until the first successful fetch since the start
    succeeded: bool = False

    def is_due(self, now: datetime) -> bool:
        """Return True if the source should be fetched at now."""
        return self.next_due is None or now >= self.next_due

    def mark_success(self, now: datetime) -> None:
        """Schedule the next regular fetch."""
        self.succeeded = True
        self.next_due = self.next_regular(now)

    def mark_failure(self, now: datetime) -> None:
        """
        Schedule a retry: every minute while nothing has worked yet.

        The regular fetch comes in any case, if it is earlier than the retry.
        """
        interval = self.retry_interval if self.succeeded else STARTUP_RETRY_INTERVAL
        self.next_due = min(now + interval, self.next_regular(now))


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class IdokepDataUpdateCoordinator(DataUpdateCoordinator):
    """
    Class to manage fetching data from the API.

    Each idokep.hu page has its own schedule:
    - current conditions (/idojaras/): every 10 minutes (x:00:59, x:10:59,
      ...), retry every minute
    - forecast (/elorejelzes/: hourly and daily forecast, alerts, today's
      max/min): every full hour (x:00:59), retry every 10 minutes
    - humidity map (/para: humidity, dew point): every 10 minutes, retry every
      minute
    - pressure map (/legnyomas: pressure): every 10 minutes, retry every minute
    - UV map (/uv: UV index): every 10 minutes, retry every minute

    After a restart or reload, a page that hasn't been fetched yet is retried
    every minute, also the forecast. A page that fails has no data until it is
    fetched again; its failure and its recovery are each logged once. The
    exception is the forecast: while its page fails, the last downloaded one
    (after a restart the saved one) is followed hour by hour, without alerts
    and warnings. While the current page fails, the forecast's running hour
    stands in for the weather entity's conditions and the storm now.

    There is no update_interval; async_tick is called every minute at second
    59 (the schedules count in whole minutes, so x:00:59 is x:00) and
    refreshes only when a source used by an enabled entity is due. A page
    whose entities are all disabled is not fetched and its data is dropped.
    """

    config_entry: IdokepConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        name: str,
        config_entry: IdokepConfigEntry,
        records: FailureRecords | None = None,
    ) -> None:
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name=name,
            update_interval=None,
            config_entry=config_entry,
        )
        self.schedules: dict[str, SourceSchedule] = {
            CURRENT: SourceSchedule(next_ten_minutes, timedelta(minutes=1)),
            HOURLY: SourceSchedule(next_full_hour, timedelta(minutes=10)),
            HUMIDITY_MAP: SourceSchedule(next_ten_minutes, timedelta(minutes=1)),
            PRESSURE_MAP: SourceSchedule(next_ten_minutes, timedelta(minutes=1)),
            UV_MAP: SourceSchedule(next_ten_minutes, timedelta(minutes=1)),
        }
        # Each source's last result; empty after a failure
        self._source_data: dict[str, dict[str, Any]] = {
            source: {} for source in self.schedules
        }
        # The settlement of the pages and of the saved forecast; another one is
        # set up by reloading the entry
        self._location: str = config_entry.data["location"]
        self._failures = {
            source: FailureLog(
                logger,
                f"Időkép {SOURCE_NAMES[source]} download for {self._location}",
                records,
            )
            for source in self.schedules
        }
        # The last downloaded forecast, saved under .storage/ (set up by
        # async_restore), and whether the page has failed since
        self._last_forecast: dict[str, Any] = {}
        self._forecast_failed = False
        self._store: Store | None = None

    @property
    def has_current_forecast(self) -> bool:
        """Return True if the forecast kept here has the hour now running."""
        return running_hour(self._last_forecast, dt_util.utcnow()) is not None

    async def async_restore(self) -> None:
        """
        Load the forecast saved before the last restart.

        Called before the first refresh. It is shown while the forecast page
        can't be downloaded, and the parser takes from it the parts a
        downloaded page no longer has, the current hour and today (see storage).
        """
        self._store = Store(
            self.hass, STORAGE_VERSION, storage_key(self.config_entry.entry_id)
        )
        saved = await self._store.async_load() or {}
        if saved.get("location", self._location) != self._location:
            # Saved before the location was changed in the options
            self.logger.debug("Saved data is for %s, not used", saved["location"])
            return

        self._last_forecast = restored_forecast(saved)
        if saved and not any(self._last_forecast.values()):
            self._failures[HOURLY].records.warn_once(
                self.logger,
                "Ignoring the saved forecast, which is unreadable: %s",
                "it has no forecast",
            )
        client = self.config_entry.runtime_data.client
        today = dt_util.as_local(dt_util.utcnow()).date()
        if item := today_item(self._last_forecast, today):
            client.restore_today_forecast(item)
        if hourly := self._last_forecast["hourly_forecast"]:
            client.restore_hourly_forecast(hourly)

    def _data_to_save(self) -> dict[str, Any]:
        """Return the data to write to disk."""
        return serialize(self._location, self._last_forecast)

    async def async_close_storage(self) -> None:
        """
        Write a scheduled save now, and save nothing after this.

        Called at unload: a save delayed beyond it would bring back the file
        of a removed entry.
        """
        store, self._store = self._store, None
        # Each downloaded forecast scheduled a save
        if store is not None and self.schedules[HOURLY].succeeded:
            await store.async_save(self._data_to_save())

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

    def _sources_at_startup(self) -> set[str]:
        """
        Return the sources to fetch before any entity listens.

        A page is left out if all of its entities are registered as disabled;
        one not registered yet is new, so it is enabled by default. Current
        conditions are always fetched, since the entry starts from them.
        """
        entry_id = self.config_entry.entry_id
        registered = {
            entity.unique_id: entity
            for entity in er.async_entries_for_config_entry(
                er.async_get(self.hass), entry_id
            )
        }
        disabled = {
            source
            for source, keys in SOURCE_ENTITIES.items()
            if all(
                (entity := registered.get(f"{entry_id}_{key}")) is not None
                and entity.disabled
                for key in keys
            )
        }
        return set(self.schedules) - (disabled - {CURRENT})

    async def async_tick(self, now: datetime) -> None:
        """Refresh if a needed source is due (called every minute at second 59)."""
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
            HUMIDITY_MAP: client.async_get_humidity_map,
            PRESSURE_MAP: client.async_get_pressure_map,
            UV_MAP: client.async_get_uv_map,
        }

    async def _async_update_data(self) -> dict:
        """
        Fetch the sources that are due and merge them with the others.

        A source that fails has no data until it is fetched again, so its
        entities are unavailable. A failing forecast page is replaced by the
        last forecast as it stands now.
        """
        now = floor_to_minute(dt_util.utcnow())
        # The first refresh runs during setup, before any entity listens; it
        # fetches every page with an enabled entity so the entry starts with data
        wanted = self._sources_at_startup() if self.data is None else self.needed_sources()
        due = [
            source
            for source, schedule in self.schedules.items()
            if source in wanted and schedule.is_due(now)
        ]
        fetchers = self._fetchers()
        results = await asyncio.gather(
            *(fetchers[source](self._location) for source in due),
            return_exceptions=True,
        )

        for source, result in zip(due, results, strict=True):
            self._take_result(source, result, now)

        # A page nobody reads anymore must not serve its old values (e.g. the
        # weather entity's humidity)
        for source in self.schedules.keys() - wanted:
            self._source_data[source] = {}

        # Applied at every update, so it moves on with the hours
        if self._forecast_failed and HOURLY in wanted:
            self._source_data[HOURLY] = forecast_as_of(self._last_forecast, now)

        data: dict[str, Any] = {}
        for source in self.schedules:
            data.update(self._source_data[source])
        # Without the current page, the forecast's running hour stands in for
        # the weather entity's conditions and for whether it's stormy now
        if not self._source_data[CURRENT] and (
            item := running_hour(self._source_data[HOURLY], now)
        ):
            data["forecast_now"] = item
            data["storm_now"] = bool(item.get("storm"))
        # Recalculated at every update, from the pages' latest values
        speed = data.get("wind_speed")
        if speed is not None:
            feels_like = apparent_temperature(
                data.get("temperature"), data.get("humidity"), speed
            )
            if feels_like is not None:
                data["apparent_temperature"] = feels_like
        return data

    def _take_result(
        self, source: str, result: dict[str, Any] | BaseException, now: datetime
    ) -> None:
        """Keep a source's fetched data and schedule its next fetch."""
        schedule = self.schedules[source]
        failures = self._failures[source]
        succeeded = False
        if isinstance(result, IdokepApiClientCommunicationError):
            failures.failed(result)
        elif isinstance(result, BaseException):
            # Unexpected, e.g. a parser error: with the traceback
            failures.failed(repr(result), exc_info=result)
        elif REQUIRED_KEYS[source] not in result:
            failures.failed(f"no {SOURCE_NAMES[source]} data on the page")
        else:
            succeeded = True

        if source == HOURLY:
            self._forecast_failed = not succeeded
        if not succeeded:
            # Nothing old is shown for a source that failed (for the forecast,
            # see _async_update_data)
            self._source_data[source] = {}
            schedule.mark_failure(now)
            return
        failures.succeeded()
        schedule.mark_success(now)
        self._source_data[source] = result
        if source == HOURLY:
            self._last_forecast = result
            if self._store is not None:
                self._store.async_delay_save(self._data_to_save, SAVE_DELAY)


def _parse_http_date(value: str | None) -> datetime | None:
    """Return an HTTP date header as an aware datetime."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


class RadarCoordinator(DataUpdateCoordinator[RadarImage | None]):
    """Keep the national radar animation up to date."""

    config_entry: RadarConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        config_entry: RadarConfigEntry,
        session: aiohttp.ClientSession,
        records: FailureRecords | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name="idokep_weather_and_forecast_radar",
            update_interval=None,
            config_entry=config_entry,
        )
        self._session = session
        self._failures = FailureLog(logger, "Időkép radar download", records)
        # Frames oldest first; the newest frame's Last-Modified value
        self._frames: deque[bytes] = deque(maxlen=FRAME_COUNT)
        self._last_modified: str | None = None
        self._last_new_frame: datetime | None = None
        # True once this 5-minute slot's new frame has arrived (no retries
        # needed until the next slot); start-up already has the latest frame
        self._got_frame_this_slot = True

    async def async_check_connection(self) -> None:
        """Check that the newest frame can be downloaded."""
        await fetch_frame(self._session, NEWEST_INDEX)

    async def async_tick(self, now: datetime) -> None:
        """
        Check for a new frame (called every minute at second 59).

        On x:00:59, x:05:59, ... always; on the other minutes only to retry
        while this slot's new frame hasn't arrived yet.
        """
        # Same rules as the forecast pages: honour "Enable polling for
        # updates" and do nothing while the radar entity is disabled
        if self.config_entry.pref_disable_polling or not self._listeners:
            return
        if now.minute % RADAR_CHECK_MINUTES == 0:
            self._got_frame_this_slot = False
        elif self._got_frame_this_slot:
            return
        await self.async_refresh()

    async def _async_update_data(self) -> RadarImage | None:
        """
        Download what changed and rebuild the animation.

        None after a failure: nothing old is shown, and the next try downloads
        all frames again.
        """
        try:
            await self._update_frames()
            image = self.data
            if image is None or image.updated != self._newest_time():
                gif = await self.hass.async_add_executor_job(
                    build_gif, list(self._frames)
                )
                image = RadarImage(gif=gif, updated=self._newest_time())
        except Exception as exception:  # pylint: disable=broad-exception-caught
            # OSError and ValueError: a frame that isn't a readable picture
            expected = (IdokepApiClientCommunicationError, OSError, ValueError)
            if isinstance(exception, expected):
                self._failures.failed(exception)
            else:
                # With the traceback
                self._failures.failed(repr(exception), exc_info=exception)
            self._frames.clear()
            self._last_modified = None
            self._got_frame_this_slot = False
            return None
        self._failures.succeeded()
        return image

    async def _update_frames(self) -> None:
        """Fetch the newest frame if it changed and shift the others along."""
        newest = await fetch_frame(self._session, NEWEST_INDEX, self._last_modified)
        if newest is None:
            return  # unchanged (304)
        if self._frames and newest.data == self._frames[-1]:
            # File rewritten with the same picture: not a new frame
            self._last_modified = newest.last_modified
            return

        now = dt_util.utcnow()
        if self._frames:
            # The previous newest frame is now radar1: just add the new one
            self._frames.append(newest.data)
        else:
            # Start-up or after a failure: get the older frames too
            older = await asyncio.gather(
                *(
                    fetch_frame(self._session, i)
                    for i in range(FRAME_COUNT - 1, NEWEST_INDEX, -1)
                )
            )
            for data in [frame.data for frame in older if frame is not None] + [
                newest.data
            ]:
                # Files still being shifted can repeat a frame: keep it once
                if not self._frames or data != self._frames[-1]:
                    self._frames.append(data)
        self._last_modified = newest.last_modified
        self._last_new_frame = now
        self._got_frame_this_slot = True

    def _newest_time(self) -> datetime:
        """Return the newest frame's time (falls back to when it was fetched)."""
        return (
            _parse_http_date(self._last_modified)
            or self._last_new_frame
            or dt_util.utcnow()
        )


class StationCoordinator(DataUpdateCoordinator[StationData]):
    """Keep a location entry's merged weather station measurements up to date."""

    config_entry: IdokepConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        config_entry: IdokepConfigEntry,
        session: aiohttp.ClientSession,
        codes: list[str],
        method: str = MERGE_PRIORITY,
        max_age: int = DEFAULT_MAX_AGE,
        records: FailureRecords | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name=f"idokep_weather_and_forecast_station_{config_entry.entry_id}",
            update_interval=None,
            config_entry=config_entry,
        )
        self._session = session
        self.codes = codes
        self.method = method
        self.max_age = max_age
        self._failures = {
            code: FailureLog(logger, f"Időkép station {code} download", records)
            for code in codes
        }
        # True while a station failed: it is retried every minute
        self.retrying = False

    async def async_check_connection(self) -> None:
        """
        Raise the first station's error if no station's file can be downloaded.

        A file that downloads but can't be read counts as downloaded; the
        first refresh reports it.
        """
        results = await asyncio.gather(
            *(fetch_station(self._session, code) for code in self.codes),
            return_exceptions=True,
        )
        if all(isinstance(r, IdokepApiClientCommunicationError) for r in results):
            raise results[0]

    async def async_tick(self, now: datetime) -> None:
        """
        Fetch on every 5th minute; retry every minute after a failure.

        Called every minute at second 59. Like the other pages: honours
        "Enable polling for updates" and skips while no station sensor is
        enabled.
        """
        if self.config_entry.pref_disable_polling or not self._listeners:
            return
        if (
            now.minute % STATION_CHECK_MINUTES
            and self.last_update_success
            and not self.retrying
        ):
            return
        await self.async_refresh()

    async def _async_update_data(self) -> StationData:
        """
        Download every station's file and merge their readings.

        A station that fails is left out; with every station failing there are
        no values, so the sensors are unavailable.
        """
        results = await asyncio.gather(
            *(fetch_station(self._session, code) for code in self.codes),
            return_exceptions=True,
        )
        now = dt_util.utcnow()
        stations: list[StationReadings] = []
        failed = False
        for code, result in zip(self.codes, results, strict=True):
            if isinstance(result, IdokepApiClientError):
                self._failures[code].failed(result)
                failed = True
            elif isinstance(result, Exception):
                # Unexpected, e.g. a decoder error: with the traceback
                self._failures[code].failed(repr(result), exc_info=result)
                failed = True
            elif isinstance(result, BaseException):
                raise result
            else:
                self._failures[code].succeeded()
                stations.append(latest_readings(result, code, now, self.max_age))
        self.retrying = failed
        return merge_readings(stations, self.method)
