"""Unit tests for the Időkép coordinator and its fetch schedules."""

from __future__ import annotations

import json
import zoneinfo
from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.idokep_weather_and_forecast.api import (
    IdokepApiClient,
    IdokepApiClientCommunicationError,
)
from custom_components.idokep_weather_and_forecast.coordinator import (
    CURRENT,
    HOURLY,
    HUMIDITY_MAP,
    PRESSURE_MAP,
    SOURCE_ENTITIES,
    UV_MAP,
    IdokepDataUpdateCoordinator,
    SourceSchedule,
    floor_to_minute,
    next_full_hour,
    next_ten_minutes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

BUDAPEST = zoneinfo.ZoneInfo("Europe/Budapest")

CURRENT_DATA = {"temperature": 15, "condition": "cloudy"}
HOURLY_DATA = {
    "hourly_forecast": [{"datetime": "x"}],
    "alerts": [],
    "daily_forecast": [{"datetime": "y"}],
}
HUMIDITY_DATA = {"humidity": 59.0, "dew_point": 2.0}
PRESSURE_DATA = {"pressure": 1029.2}
UV_DATA = {"uv_index": 0.3}
MAP_DATA = {**HUMIDITY_DATA, **PRESSURE_DATA, **UV_DATA}
ALL_SOURCES = {CURRENT, HOURLY, HUMIDITY_MAP, PRESSURE_MAP, UV_MAP}


@pytest.fixture(autouse=True)
def registered_entities() -> Iterator[list[Mock]]:
    """Patch the entity registry; tests add the entities registered before."""
    entities: list[Mock] = []
    with (
        patch("custom_components.idokep_weather_and_forecast.coordinator.er.async_get"),
        patch(
            "custom_components.idokep_weather_and_forecast.coordinator.er"
            ".async_entries_for_config_entry",
            return_value=entities,
        ),
    ):
        yield entities


@pytest.fixture(autouse=True)
def budapest_time_zone() -> Iterator[None]:
    """Run tests with Home Assistant set to the Budapest time zone."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(BUDAPEST)
    yield
    dt_util.set_default_time_zone(original)


def local(*args: int) -> datetime:
    """Return a Budapest-local datetime."""
    return datetime(*args, tzinfo=BUDAPEST)


@pytest.fixture
def client() -> Mock:
    """Return an API client whose page fetches all succeed."""
    client = Mock()
    client.async_get_current_weather = AsyncMock(return_value=CURRENT_DATA)
    client.async_get_hourly_forecast = AsyncMock(return_value=HOURLY_DATA)
    client.async_get_humidity_map = AsyncMock(return_value=HUMIDITY_DATA)
    client.async_get_pressure_map = AsyncMock(return_value=PRESSURE_DATA)
    client.async_get_uv_map = AsyncMock(return_value=UV_DATA)
    return client


@pytest.fixture
def coordinator(mock_hass: Mock, client: Mock) -> IdokepDataUpdateCoordinator:
    """Return a coordinator for Budapest using the mock client."""
    config_entry = Mock()
    config_entry.data = {"location": "Budapest"}
    config_entry.runtime_data.client = client
    config_entry.pref_disable_polling = False
    return IdokepDataUpdateCoordinator(
        mock_hass, getLogger(__name__), "test", config_entry
    )


def listen(coordinator: IdokepDataUpdateCoordinator, *sources: str) -> None:
    """Register an entity-like listener that reads the given pages."""
    coordinator.async_add_listener(lambda: None, frozenset(sources))


async def update_at(
    coordinator: IdokepDataUpdateCoordinator, now: datetime
) -> dict[str, Any]:
    """Run one coordinator update as if it were now."""
    with patch.object(dt_util, "utcnow", return_value=dt_util.as_utc(now)):
        return await coordinator._async_update_data()


class TestScheduleTimes:
    """Next-fetch time calculations."""

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (local(2026, 10, 29, 12, 7), local(2026, 10, 29, 12, 10)),
            (local(2026, 10, 29, 12, 10), local(2026, 10, 29, 12, 20)),
            (local(2026, 10, 29, 12, 59, 59), local(2026, 10, 29, 13, 0)),
            (local(2026, 10, 29, 23, 50), local(2026, 10, 30, 0, 0)),
        ],
    )
    def test_next_ten_minutes(self, now: datetime, expected: datetime) -> None:
        """Current conditions run at x:00, x:10, x:20, x:30, x:40 and x:50."""
        assert next_ten_minutes(now) == expected

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (local(2026, 10, 29, 12, 0), local(2026, 10, 29, 13, 0)),
            (local(2026, 10, 29, 12, 40), local(2026, 10, 29, 13, 0)),
            (local(2026, 10, 29, 23, 5), local(2026, 10, 30, 0, 0)),
        ],
    )
    def test_next_full_hour(self, now: datetime, expected: datetime) -> None:
        """Hourly forecast runs at x:00."""
        assert next_full_hour(now) == expected

    def test_floor_to_minute(self) -> None:
        """Timer jitter (seconds, microseconds) is dropped."""
        assert floor_to_minute(local(2026, 10, 29, 12, 15, 0, 4000)) == local(
            2026, 10, 29, 12, 15
        )


class TestSourceSchedule:
    """Due checks, success and retry handling."""

    def test_due_until_first_fetch(self) -> None:
        """A source with no fetch yet is due immediately."""
        schedule = SourceSchedule(next_full_hour, timedelta(minutes=10))
        assert schedule.is_due(local(2026, 10, 29, 12, 7))

    def test_success_and_failure(self) -> None:
        """Success waits for the next boundary, failure for the retry interval."""
        schedule = SourceSchedule(next_full_hour, timedelta(minutes=10))
        now = local(2026, 10, 29, 12, 0)

        schedule.mark_success(now)
        assert schedule.next_due == local(2026, 10, 29, 13, 0)
        assert not schedule.is_due(local(2026, 10, 29, 12, 59))
        assert schedule.is_due(local(2026, 10, 29, 13, 0))

        schedule.mark_failure(now)
        assert schedule.next_due == local(2026, 10, 29, 12, 10)


class TestCoordinatorUpdates:
    """Fetching only what is due and keeping the rest."""

    def test_no_polling_interval(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """Refreshes are driven by the minute ticks, not update_interval."""
        assert coordinator.update_interval is None

    @pytest.mark.asyncio
    async def test_first_update_fetches_all_pages(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """The first update fetches every page and merges the results."""
        data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

        assert data == {**CURRENT_DATA, **HOURLY_DATA, **MAP_DATA}
        client.async_get_current_weather.assert_awaited_once_with("Budapest")
        client.async_get_hourly_forecast.assert_awaited_once_with("Budapest")
        client.async_get_humidity_map.assert_awaited_once_with("Budapest")
        client.async_get_pressure_map.assert_awaited_once_with("Budapest")
        client.async_get_uv_map.assert_awaited_once_with("Budapest")
        assert set(coordinator.schedules) == ALL_SOURCES
        assert coordinator.schedules[CURRENT].next_due == local(2026, 10, 29, 12, 10)
        assert coordinator.schedules[HUMIDITY_MAP].next_due == local(2026, 10, 29, 12, 10)
        assert coordinator.schedules[PRESSURE_MAP].next_due == local(2026, 10, 29, 12, 10)
        assert coordinator.schedules[UV_MAP].next_due == local(2026, 10, 29, 12, 10)
        assert coordinator.schedules[HOURLY].next_due == local(2026, 10, 29, 13, 0)

    @pytest.mark.asyncio
    async def test_later_update_fetches_only_due_pages(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """At 12:15 only current conditions are fetched; others are reused."""
        await update_at(coordinator, local(2026, 10, 29, 12, 7))
        client.async_get_current_weather.return_value = {"temperature": 16}

        data = await update_at(coordinator, local(2026, 10, 29, 12, 15))

        assert client.async_get_current_weather.await_count == 2
        assert client.async_get_hourly_forecast.await_count == 1
        assert data["temperature"] == 16
        assert data["hourly_forecast"] == HOURLY_DATA["hourly_forecast"]
        assert data["daily_forecast"] == HOURLY_DATA["daily_forecast"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source", "fetcher", "retry_at"),
        [
            (CURRENT, "async_get_current_weather", local(2026, 10, 29, 12, 8)),
            (HOURLY, "async_get_hourly_forecast", local(2026, 10, 29, 12, 17)),
            (HUMIDITY_MAP, "async_get_humidity_map", local(2026, 10, 29, 12, 8)),
            (PRESSURE_MAP, "async_get_pressure_map", local(2026, 10, 29, 12, 8)),
            (UV_MAP, "async_get_uv_map", local(2026, 10, 29, 12, 8)),
        ],
    )
    async def test_failed_fetch_is_retried(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        source: str,
        fetcher: str,
        retry_at: datetime,
    ) -> None:
        """Failures retry after 1 min / 10 min respectively."""
        getattr(client, fetcher).side_effect = IdokepApiClientCommunicationError("down")

        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        assert coordinator.schedules[source].next_due == retry_at

    @pytest.mark.asyncio
    async def test_failure_keeps_last_good_data(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """A failed page keeps serving its previous data."""
        await update_at(coordinator, local(2026, 10, 29, 12, 7))
        client.async_get_current_weather.side_effect = (
            IdokepApiClientCommunicationError("down")
        )

        data = await update_at(coordinator, local(2026, 10, 29, 12, 15))

        assert data["temperature"] == CURRENT_DATA["temperature"]
        assert coordinator.schedules[CURRENT].next_due == local(2026, 10, 29, 12, 16)

    @pytest.mark.asyncio
    async def test_page_without_data_is_a_failure(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """A page missing its main data counts as failed and is retried."""
        client.async_get_hourly_forecast.return_value = {}

        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        assert coordinator.schedules[HOURLY].next_due == local(2026, 10, 29, 12, 17)

    @pytest.mark.asyncio
    async def test_unexpected_error_is_a_failure(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """A parser crash only affects its own page."""
        client.async_get_hourly_forecast.side_effect = ValueError("bad html")

        data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

        assert coordinator.schedules[HOURLY].next_due == local(2026, 10, 29, 12, 17)
        assert data["temperature"] == CURRENT_DATA["temperature"]

    @pytest.mark.asyncio
    async def test_update_fails_without_any_data(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """With nothing ever fetched the update fails."""
        error = IdokepApiClientCommunicationError("down")
        client.async_get_current_weather.side_effect = error
        client.async_get_hourly_forecast.side_effect = error
        client.async_get_humidity_map.side_effect = error
        client.async_get_pressure_map.side_effect = error
        client.async_get_uv_map.side_effect = error

        with pytest.raises(UpdateFailed, match="No weather data found"):
            await update_at(coordinator, local(2026, 10, 29, 12, 7))


class TestTick:
    """The whole-minute timer callback."""

    @pytest.mark.asyncio
    async def test_tick_refreshes_only_when_due(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """Ticks between scheduled times do nothing."""
        listen(coordinator, CURRENT, HOURLY)
        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        with patch.object(coordinator, "async_refresh", AsyncMock()) as refresh:
            await coordinator.async_tick(local(2026, 10, 29, 12, 9))
            refresh.assert_not_awaited()

            # The timer fires a few milliseconds late; that must still count
            await coordinator.async_tick(local(2026, 10, 29, 12, 10, 0, 4000))
            refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tick_does_nothing_without_listeners(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """With every entity disabled (e.g. a disabled device) nothing is fetched."""
        with patch.object(coordinator, "async_refresh", AsyncMock()) as refresh:
            # Every page is due, but nobody listens
            await coordinator.async_tick(local(2026, 10, 29, 12, 15))
        refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_respects_disabled_polling(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """The entry's "Enable polling for updates" option stops the timer."""
        listen(coordinator, CURRENT, HOURLY)
        coordinator.config_entry.pref_disable_polling = True

        with patch.object(coordinator, "async_refresh", AsyncMock()) as refresh:
            await coordinator.async_tick(local(2026, 10, 29, 12, 15))
        refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_ignores_due_pages_nobody_needs(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """Only pages an enabled entity reads can trigger a refresh."""
        await update_at(coordinator, local(2026, 10, 29, 12, 7))
        listen(coordinator, HOURLY)  # e.g. only the alert sensors enabled

        with patch.object(coordinator, "async_refresh", AsyncMock()) as refresh:
            # 12:15 and 12:30: current conditions are due but nobody needs them
            await coordinator.async_tick(local(2026, 10, 29, 12, 15))
            await coordinator.async_tick(local(2026, 10, 29, 12, 30))
            refresh.assert_not_awaited()
            # 13:00: the forecast page is due and needed
            await coordinator.async_tick(local(2026, 10, 29, 13, 0))
            refresh.assert_awaited_once()


class TestNeededSources:
    """Fetching only the pages enabled entities use."""

    def test_union_of_listener_contexts(
        self, coordinator: IdokepDataUpdateCoordinator
    ) -> None:
        """Needed pages are the union of what the listening entities read."""
        assert coordinator.needed_sources() == set()
        listen(coordinator, CURRENT)
        listen(coordinator, HOURLY)
        listen(coordinator, HOURLY)
        assert coordinator.needed_sources() == {CURRENT, HOURLY}

    @pytest.mark.asyncio
    async def test_first_refresh_fetches_everything(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """Setup runs before entities listen; with nothing disabled, all pages."""
        assert coordinator.needed_sources() == set()

        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        client.async_get_current_weather.assert_awaited_once()
        client.async_get_hourly_forecast.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_later_refresh_skips_unneeded_pages(
        self, coordinator: IdokepDataUpdateCoordinator, client: Mock
    ) -> None:
        """After setup, due pages without an enabled entity are not fetched."""
        coordinator.data = await update_at(coordinator, local(2026, 10, 29, 12, 7))
        listen(coordinator, CURRENT)  # no forecast-page entity enabled

        # 13:00: current and hourly are due, only current is needed
        await update_at(coordinator, local(2026, 10, 29, 13, 0))

        assert client.async_get_current_weather.await_count == 2
        assert client.async_get_hourly_forecast.await_count == 1
        # The skipped page stays due for when an entity needs it again
        assert coordinator.schedules[HOURLY].is_due(local(2026, 10, 29, 13, 0))


class FakeStore:
    """Stand-in for Home Assistant's Store: keeps the file content in memory."""

    files: dict[str, object] = {}  # shared "disk" for a test

    def __init__(self, _hass: object, _version: int, key: str) -> None:
        """Open the "file" for key."""
        self.key = key

    async def async_load(self) -> object:
        """Return the saved content, or None."""
        return self.files.get(self.key)

    def async_delay_save(self, data_func: object, _delay: float) -> None:
        """Save right away (a real Store waits a few seconds)."""
        self.files[self.key] = json.loads(json.dumps(data_func()))


@pytest.fixture
def fake_store() -> Iterator[type[FakeStore]]:
    """Replace the coordinator's Store with an in-memory one."""
    FakeStore.files = {}
    with patch(
        "custom_components.idokep_weather_and_forecast.coordinator.Store", FakeStore
    ):
        yield FakeStore


class TestSavedData:
    """Saving fetched pages and restoring them after a restart."""

    @pytest.mark.asyncio
    async def test_fetch_saves_each_page(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        fake_store: type[FakeStore],
    ) -> None:
        """Successful fetches are written to the entry's file."""
        coordinator.config_entry.entry_id = "entry1"
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 12, 7))
        ):
            await coordinator.async_restore()  # nothing saved yet
        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        saved = fake_store.files["idokep_weather_and_forecast.entry1"]
        assert saved["location"] == "Budapest"
        sources = saved["sources"]
        assert set(sources) == ALL_SOURCES
        assert sources[CURRENT]["data"] == CURRENT_DATA
        assert datetime.fromisoformat(sources[CURRENT]["fetched"]) == local(
            2026, 10, 29, 12, 7
        )

    @pytest.mark.asyncio
    async def test_failed_fetch_keeps_saved_copy(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        fake_store: type[FakeStore],
    ) -> None:
        """A failing page does not overwrite its saved data."""
        coordinator.config_entry.entry_id = "entry1"
        await coordinator.async_restore()
        await update_at(coordinator, local(2026, 10, 29, 12, 7))
        client.async_get_current_weather.side_effect = (
            IdokepApiClientCommunicationError("down")
        )
        await update_at(coordinator, local(2026, 10, 29, 12, 15))

        sources = fake_store.files["idokep_weather_and_forecast.entry1"]["sources"]
        assert sources[CURRENT]["data"] == CURRENT_DATA

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fake_store")
    async def test_restart_while_site_is_down(
        self,
        mock_hass: Mock,
        client: Mock,
        coordinator: IdokepDataUpdateCoordinator,
    ) -> None:
        """After a restart with idokep.hu down, the saved data is served."""
        hourly = {
            "hourly_forecast": [
                {"datetime": local(2026, 10, 29, 12, 0).isoformat(), "temperature": 14},
                {"datetime": local(2026, 10, 29, 13, 0).isoformat(), "temperature": 15},
            ],
            "alerts": [],
            "daily_forecast": [{"datetime": "2026-10-29", "temperature": 16}],
        }
        client.async_get_hourly_forecast.return_value = hourly
        coordinator.config_entry.entry_id = "entry1"
        await coordinator.async_restore()
        await update_at(coordinator, local(2026, 10, 29, 12, 7))

        # "Restart": a new coordinator, and every fetch fails
        error = IdokepApiClientCommunicationError("down")
        client.async_get_current_weather.side_effect = error
        client.async_get_hourly_forecast.side_effect = error
        restarted = IdokepDataUpdateCoordinator(
            mock_hass, getLogger(__name__), "test", coordinator.config_entry
        )
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 12, 30))
        ):
            await restarted.async_restore()
        data = await update_at(restarted, local(2026, 10, 29, 12, 30))

        # No UpdateFailed: the entry starts with the saved data
        assert data == {**CURRENT_DATA, **hourly, **MAP_DATA}

    @pytest.mark.asyncio
    async def test_restore_seeds_todays_min_max(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        fake_store: type[FakeStore],
    ) -> None:
        """Today's max/min survive an evening restart."""
        coordinator.config_entry.entry_id = "entry1"
        fake_store.files["idokep_weather_and_forecast.entry1"] = {
            "sources": {
                HOURLY: {
                    "fetched": local(2026, 10, 29, 15, 0).isoformat(),
                    "data": {
                        "hourly_forecast": [],
                        "wind_bearing": 10,
                        "temperature_max_today": 16,
                        "temperature_min_today": 6,
                    },
                }
            }
        }
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 21, 0))
        ):
            await coordinator.async_restore()

        # Six hours old: the wind is dropped, today's max/min kept
        assert coordinator._source_data[HOURLY] == {
            "temperature_max_today": 16,
            "temperature_min_today": 6,
        }
        # Saved by an earlier version: only max/min, so a minimal entry
        client.restore_today_forecast.assert_called_once_with(
            {
                "datetime": "2026-10-29",
                "temperature": 16,
                "templow": 6,
                "condition": None,
                "precipitation": 0,
                "precipitation_probability": 0,
            }
        )

    @pytest.mark.asyncio
    async def test_restore_seeds_todays_daily_entry(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        fake_store: type[FakeStore],
    ) -> None:
        """Today's full daily entry survives an evening restart."""
        today = {"datetime": "2026-10-29", "temperature": 16, "templow": 6}
        tomorrow = {"datetime": "2026-10-30", "temperature": 14, "templow": 7}
        coordinator.config_entry.entry_id = "entry1"
        fake_store.files["idokep_weather_and_forecast.entry1"] = {
            "sources": {
                HOURLY: {
                    "fetched": local(2026, 10, 29, 20, 0).isoformat(),
                    "data": {
                        "daily_forecast": [today, tomorrow],
                        "temperature_max_today": 16,
                        "temperature_min_today": 6,
                    },
                }
            }
        }
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 21, 0))
        ):
            await coordinator.async_restore()

        client.restore_today_forecast.assert_called_once_with(today)

    @pytest.mark.asyncio
    async def test_restore_seeds_hourly_forecast(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        fake_store: type[FakeStore],
    ) -> None:
        """The hours not over are handed to the client (to keep the current hour)."""
        past = {"datetime": local(2026, 10, 30, 11, 0).isoformat()}
        current = {"datetime": local(2026, 10, 30, 12, 0).isoformat()}
        coordinator.config_entry.entry_id = "entry1"
        fake_store.files["idokep_weather_and_forecast.entry1"] = {
            "sources": {
                HOURLY: {
                    "fetched": local(2026, 10, 30, 11, 0).isoformat(),
                    "data": {"hourly_forecast": [past, current]},
                }
            }
        }
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 30, 12, 51))
        ):
            await coordinator.async_restore()

        client.restore_hourly_forecast.assert_called_once_with([current])

    @pytest.mark.asyncio
    async def test_restore_skips_data_of_another_location(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        client: Mock,
        fake_store: type[FakeStore],
    ) -> None:
        """Data saved before the location was changed is not restored."""
        coordinator.config_entry.entry_id = "entry1"
        fake_store.files["idokep_weather_and_forecast.entry1"] = {
            "location": "Debrecen-Józsa",
            "sources": {
                CURRENT: {
                    "fetched": local(2026, 10, 30, 12, 45).isoformat(),
                    "data": CURRENT_DATA,
                }
            },
        }
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 30, 12, 51))
        ):
            await coordinator.async_restore()

        assert coordinator._source_data[CURRENT] == {}
        client.restore_hourly_forecast.assert_not_called()


@pytest.mark.asyncio
async def test_restore_ignores_unknown_and_expired_pages(
    coordinator: IdokepDataUpdateCoordinator,
    fake_store: type[FakeStore],
) -> None:
    """Unknown page names and pages with nothing still valid are not restored."""
    coordinator.config_entry.entry_id = "entry1"
    fake_store.files["idokep_weather_and_forecast.entry1"] = {
        "location": "Budapest",
        "sources": {
            "unknown_page": {
                "fetched": local(2026, 10, 30, 12, 45).isoformat(),
                "data": {},
            },
            # Saved the day before: current conditions are kept for an hour only
            CURRENT: {
                "fetched": local(2026, 10, 29, 12, 45).isoformat(),
                "data": CURRENT_DATA,
            },
        },
    }
    with patch.object(
        dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 30, 12, 51))
    ):
        await coordinator.async_restore()

    assert coordinator._source_data[CURRENT] == {}
    assert "unknown_page" not in coordinator._source_data


@pytest.mark.asyncio
async def test_settlement_without_stations_is_not_retried(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """No station for the settlement is a normal result, not a failed fetch."""
    client.async_get_humidity_map.return_value = {"humidity": None, "dew_point": None}
    client.async_get_pressure_map.return_value = {"pressure": None}
    client.async_get_uv_map.return_value = {"uv_index": None}

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    assert data["humidity"] is None
    assert data["pressure"] is None
    assert coordinator.schedules[HUMIDITY_MAP].next_due == local(2026, 10, 29, 12, 10)
    assert coordinator.schedules[PRESSURE_MAP].next_due == local(2026, 10, 29, 12, 10)
    assert coordinator.schedules[UV_MAP].next_due == local(2026, 10, 29, 12, 10)


@pytest.mark.asyncio
async def test_map_pages_fail_and_retry_separately(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """A failed pressure page leaves humidity on schedule; only it is retried."""
    client.async_get_pressure_map.side_effect = IdokepApiClientCommunicationError("down")

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    assert data["humidity"] == HUMIDITY_DATA["humidity"]
    assert "pressure" not in data
    assert coordinator.schedules[HUMIDITY_MAP].next_due == local(2026, 10, 29, 12, 10)
    assert coordinator.schedules[PRESSURE_MAP].next_due == local(2026, 10, 29, 12, 8)

    client.async_get_pressure_map.side_effect = None
    data = await update_at(coordinator, local(2026, 10, 29, 12, 8))

    assert data["pressure"] == PRESSURE_DATA["pressure"]
    assert client.async_get_humidity_map.await_count == 1
    assert client.async_get_pressure_map.await_count == 2


def _registered(entry_id: str, keys: tuple[str, ...], *, disabled: bool) -> list[Mock]:
    return [Mock(unique_id=f"{entry_id}_{key}", disabled=disabled) for key in keys]


@pytest.mark.asyncio
async def test_startup_skips_a_page_with_all_entities_disabled(
    coordinator: IdokepDataUpdateCoordinator,
    client: Mock,
    registered_entities: list[Mock],
) -> None:
    """A restart doesn't fetch a page whose entities are all disabled."""
    coordinator.config_entry.entry_id = "entry1"
    registered_entities.extend(
        _registered("entry1", SOURCE_ENTITIES[HUMIDITY_MAP], disabled=True)
    )

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    client.async_get_humidity_map.assert_not_awaited()
    client.async_get_pressure_map.assert_awaited_once()
    assert "humidity" not in data
    assert data["pressure"] == PRESSURE_DATA["pressure"]


@pytest.mark.asyncio
async def test_startup_fetches_a_page_with_one_entity_enabled(
    coordinator: IdokepDataUpdateCoordinator,
    client: Mock,
    registered_entities: list[Mock],
) -> None:
    """One enabled sensor is enough; so is one not registered yet (new sensor)."""
    coordinator.config_entry.entry_id = "entry1"
    registered_entities.extend(
        [
            *_registered("entry1", ("humidity",), disabled=True),
            *_registered("entry1", ("dew_point",), disabled=False),
            *_registered("entry1", SOURCE_ENTITIES[HOURLY][1:], disabled=True),
        ]
    )

    await update_at(coordinator, local(2026, 10, 29, 12, 7))

    client.async_get_humidity_map.assert_awaited_once()
    client.async_get_hourly_forecast.assert_awaited_once()  # "weather" is missing


@pytest.mark.asyncio
async def test_startup_always_fetches_current_conditions(
    coordinator: IdokepDataUpdateCoordinator,
    client: Mock,
    registered_entities: list[Mock],
) -> None:
    """Even with every entity disabled, the entry starts from current conditions."""
    coordinator.config_entry.entry_id = "entry1"
    for keys in SOURCE_ENTITIES.values():
        registered_entities.extend(_registered("entry1", keys, disabled=True))

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    client.async_get_current_weather.assert_awaited_once()
    client.async_get_hourly_forecast.assert_not_awaited()
    client.async_get_uv_map.assert_not_awaited()
    assert data == CURRENT_DATA


@pytest.mark.asyncio
async def test_disabling_a_pages_sensors_stops_it_and_drops_its_values(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """Once nothing reads a page: no more fetches, and no old values are served."""
    coordinator.data = await update_at(coordinator, local(2026, 10, 29, 12, 7))
    listen(coordinator, CURRENT, HOURLY, PRESSURE_MAP, UV_MAP)  # humidity disabled

    data = await update_at(coordinator, local(2026, 10, 29, 12, 10))

    assert client.async_get_humidity_map.await_count == 1
    assert client.async_get_pressure_map.await_count == 2
    assert "humidity" not in data
    assert "dew_point" not in data
    assert data["pressure"] == PRESSURE_DATA["pressure"]
    assert HUMIDITY_MAP not in coordinator._fetched


@pytest.mark.asyncio
async def test_restored_values_of_a_disabled_page_are_dropped(
    coordinator: IdokepDataUpdateCoordinator,
    registered_entities: list[Mock],
    fake_store: type[FakeStore],
) -> None:
    """Values saved before the sensors were disabled don't come back."""
    coordinator.config_entry.entry_id = "entry1"
    registered_entities.extend(_registered("entry1", ("pressure",), disabled=True))
    fake_store.files["idokep_weather_and_forecast.entry1"] = {
        "location": "Budapest",
        "sources": {
            PRESSURE_MAP: {
                "fetched": local(2026, 10, 29, 12, 0).isoformat(),
                "data": PRESSURE_DATA,
            },
        },
    }
    with patch.object(
        dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 12, 7))
    ):
        await coordinator.async_restore()

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    assert "pressure" not in data


@pytest.mark.asyncio
async def test_apparent_temperature_is_recalculated_at_every_update(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """Each update uses the pages' latest values, even if only one page changed."""
    client.async_get_hourly_forecast.return_value = {
        **HOURLY_DATA,
        "wind_speed": 14,
        "wind_gust_speed": 22,  # their mean is 18 km/h
    }
    client.async_get_current_weather.return_value = {"temperature": 20}
    client.async_get_humidity_map.return_value = {"humidity": 50.0, "dew_point": 9.0}

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))
    assert data["apparent_temperature"] == 16.35

    # 12:10: only the 10-minute pages are due, the wind speed stays
    client.async_get_current_weather.return_value = {"temperature": 25}
    coordinator.data = data
    listen(coordinator, CURRENT, HOURLY, HUMIDITY_MAP)
    data = await update_at(coordinator, local(2026, 10, 29, 12, 10))

    assert client.async_get_hourly_forecast.await_count == 1
    assert data["apparent_temperature"] > 16.35


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "humidity", "wind"),
    [
        ({"temperature": 20}, {"humidity": None, "dew_point": None}, 18),
        ({"temperature": 20}, {"humidity": 50.0, "dew_point": 9.0}, None),
    ],
)
async def test_no_apparent_temperature_without_an_input(
    coordinator: IdokepDataUpdateCoordinator,
    client: Mock,
    current: dict,
    humidity: dict,
    wind: int | None,
) -> None:
    """Without humidity or wind speed there is no value (the sensor is unavailable)."""
    client.async_get_current_weather.return_value = current
    client.async_get_humidity_map.return_value = humidity
    client.async_get_hourly_forecast.return_value = {
        **HOURLY_DATA,
        **({} if wind is None else {"wind_speed": wind, "wind_gust_speed": wind}),
    }

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    assert "apparent_temperature" not in data


@pytest.mark.asyncio
async def test_apparent_temperature_sensor_keeps_its_pages_going(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """With only this sensor enabled, all three of its pages are still fetched."""
    coordinator.data = await update_at(coordinator, local(2026, 10, 29, 12, 7))
    listen(coordinator, CURRENT, HOURLY, HUMIDITY_MAP)

    await update_at(coordinator, local(2026, 10, 29, 13, 0))

    assert client.async_get_current_weather.await_count == 2
    assert client.async_get_hourly_forecast.await_count == 2
    assert client.async_get_humidity_map.await_count == 2
    assert client.async_get_pressure_map.await_count == 1


@pytest.mark.asyncio
async def test_no_apparent_temperature_without_the_gust(
    coordinator: IdokepDataUpdateCoordinator, client: Mock
) -> None:
    """Both wind values are needed for their mean."""
    client.async_get_hourly_forecast.return_value = {**HOURLY_DATA, "wind_speed": 20}

    data = await update_at(coordinator, local(2026, 10, 29, 12, 7))

    assert "apparent_temperature" not in data


@pytest.mark.asyncio
async def test_upgrade_mid_hour_has_gust_and_apparent_temperature(
    coordinator: IdokepDataUpdateCoordinator, fake_store: type[FakeStore]
) -> None:
    """Data saved by an earlier version gets the current wind estimates at startup."""
    client = IdokepApiClient(Mock())
    coordinator.config_entry.runtime_data.client = client
    coordinator.config_entry.entry_id = "entry1"
    fetched = local(2026, 10, 29, 12, 0).isoformat()
    fake_store.files["idokep_weather_and_forecast.entry1"] = {
        "location": "Budapest",
        "sources": {
            HOURLY: {
                "fetched": fetched,
                "data": {
                    "wind_strength": "gentle_breeze",
                    "wind_speed": 16,
                    "hourly_forecast": [
                        {
                            "datetime": local(2026, 10, 29, 12, 0).isoformat(),
                            "wind_strength": "gentle_breeze",
                            "native_wind_speed": 16,
                        }
                    ],
                },
            },
        },
    }
    with patch.object(
        dt_util, "utcnow", return_value=dt_util.as_utc(local(2026, 10, 29, 12, 8))
    ):
        await coordinator.async_restore()

    assert coordinator._source_data[HOURLY]["wind_speed"] == 12
    assert coordinator._source_data[HOURLY]["wind_gust_speed"] == 19
    kept = client._hourly_parser._last_forecast[0]
    assert kept["native_wind_speed"] == 12
    assert kept["native_wind_gust_speed"] == 19
