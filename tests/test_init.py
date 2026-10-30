"""Unit tests for the Időkép integration __init__.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.idokep_weather_and_forecast import (
    PLATFORMS,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.idokep_weather_and_forecast.const import DOMAIN


class TestIdokepInit:
    """Test cases for Időkép integration initialization."""

    def test_platforms_constant(self) -> None:
        """Test that PLATFORMS constant is correctly defined."""
        expected_platforms = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.WEATHER]
        assert expected_platforms == PLATFORMS

    @pytest.mark.asyncio
    async def test_async_setup_entry_success(self) -> None:
        """Test successful setup of config entry."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        # No Radar entry yet; started flows are not run in this test
        mock_hass.config_entries.async_entries = Mock(return_value=[])
        mock_hass.async_create_task = Mock(side_effect=lambda coro: coro.close())
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.domain = DOMAIN
        mock_entry.entry_id = "test_entry_id"
        mock_entry.data = {"location": "Budapest"}
        mock_entry.options = {}

        # Mock runtime data assignment
        mock_entry.runtime_data = None
        mock_entry.async_on_unload = Mock()

        # Mock coordinator
        mock_coordinator = AsyncMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()

        # Mock API client
        mock_client = Mock()

        with (
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepDataUpdateCoordinator"
            ) as mock_coordinator_class,
            patch(
                "custom_components.idokep_weather_and_forecast.async_get_clientsession"
            ) as mock_get_session,
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepApiClient"
            ) as mock_api_client_class,
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepData"
            ) as mock_data_class,
            patch(
                "custom_components.idokep_weather_and_forecast.async_track_time_change",
                return_value="unsub_timer",
            ) as mock_track_time,
            patch(
                "custom_components.idokep_weather_and_forecast.dr.async_get"
            ) as mock_dr_get,
        ):
            # No station device left from before
            mock_dr_get.return_value.async_get_device.return_value = None
            # Configure mocks
            mock_coordinator_class.return_value = mock_coordinator
            mock_get_session.return_value = "session"
            mock_api_client_class.return_value = mock_client
            mock_data_class.return_value = "data"

            # Mock forward_entry_setups
            mock_hass.config_entries.async_forward_entry_setups = AsyncMock(
                return_value=True
            )

            # Call the function
            result = await async_setup_entry(mock_hass, mock_entry)

            # Assertions
            assert result is True
            mock_coordinator_class.assert_called_once()
            mock_coordinator.async_config_entry_first_refresh.assert_called_once()
            # A location's setup creates the Radar entry when it is missing
            mock_hass.config_entries.flow.async_init.assert_called_once_with(
                DOMAIN, context={"source": "import"}
            )
            # Saved data is restored before the first fetch
            call_names = [c[0] for c in mock_coordinator.mock_calls]
            assert call_names.index("async_restore") < call_names.index(
                "async_config_entry_first_refresh"
            )
            mock_hass.config_entries.async_forward_entry_setups.assert_called_once_with(
                mock_entry, PLATFORMS
            )
            # Schedules are checked at every whole minute, stopped on unload
            mock_track_time.assert_called_once_with(
                mock_hass, mock_coordinator.async_tick, second=0
            )
            mock_entry.async_on_unload.assert_called_once_with("unsub_timer")

    @staticmethod
    def _location_entry(options: dict) -> tuple[Mock, Mock]:
        """Return (hass, entry) mocks for a location entry's setup."""
        mock_hass = Mock(spec=HomeAssistant)
        mock_hass.config_entries.async_entries = Mock(return_value=[])
        mock_hass.async_create_task = Mock(side_effect=lambda coro: coro.close())
        mock_hass.config_entries.async_forward_entry_setups = AsyncMock()
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.domain = DOMAIN
        mock_entry.entry_id = "entry1"
        mock_entry.data = {"location": "Debrecen-Józsa", "station": "jozsamet"}
        mock_entry.options = options
        mock_entry.async_on_unload = Mock()
        return mock_hass, mock_entry

    @pytest.mark.asyncio
    async def test_async_setup_entry_with_station(self) -> None:
        """A station code adds the station coordinator and its timer."""
        mock_hass, mock_entry = self._location_entry({})
        station = Mock()
        station.async_refresh = AsyncMock()

        with (
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepDataUpdateCoordinator",
                return_value=AsyncMock(),
            ),
            patch(
                "custom_components.idokep_weather_and_forecast.async_get_clientsession"
            ),
            patch("custom_components.idokep_weather_and_forecast.IdokepApiClient"),
            patch(
                "custom_components.idokep_weather_and_forecast.StationCoordinator",
                return_value=station,
            ) as mock_station_class,
            patch(
                "custom_components.idokep_weather_and_forecast.async_track_time_change"
            ) as mock_track_time,
        ):
            assert await async_setup_entry(mock_hass, mock_entry)

        assert mock_station_class.call_args.kwargs["code"] == "jozsamet"
        assert mock_entry.runtime_data.station is station
        # A failed station fetch must not fail the entry: no first refresh
        station.async_refresh.assert_awaited_once()
        mock_track_time.assert_any_call(mock_hass, station.async_tick, second=59)

    @pytest.mark.asyncio
    async def test_async_setup_entry_station_removed_in_options(self) -> None:
        """An emptied station code removes the station device."""
        mock_hass, mock_entry = self._location_entry({"station": ""})

        with (
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepDataUpdateCoordinator",
                return_value=AsyncMock(),
            ),
            patch(
                "custom_components.idokep_weather_and_forecast.async_get_clientsession"
            ),
            patch("custom_components.idokep_weather_and_forecast.IdokepApiClient"),
            patch(
                "custom_components.idokep_weather_and_forecast.StationCoordinator"
            ) as mock_station_class,
            patch(
                "custom_components.idokep_weather_and_forecast.async_track_time_change"
            ),
            patch(
                "custom_components.idokep_weather_and_forecast.dr.async_get"
            ) as mock_dr_get,
        ):
            registry = mock_dr_get.return_value
            registry.async_get_device.return_value = Mock(id="device1")
            assert await async_setup_entry(mock_hass, mock_entry)

        mock_station_class.assert_not_called()
        assert mock_entry.runtime_data.station is None
        registry.async_get_device.assert_called_once_with(
            identifiers={(DOMAIN, "entry1_station")}
        )
        registry.async_update_device.assert_called_once_with(
            "device1", remove_config_entry_id="entry1"
        )

    @pytest.mark.asyncio
    async def test_async_setup_entry_coordinator_failure(self) -> None:
        """Test setup failure when coordinator first refresh fails."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        # No Radar entry yet; started flows are not run in this test
        mock_hass.config_entries.async_entries = Mock(return_value=[])
        mock_hass.async_create_task = Mock(side_effect=lambda coro: coro.close())
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.domain = DOMAIN
        mock_entry.data = {"location": "Budapest"}

        # Mock coordinator that fails
        mock_coordinator = AsyncMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock(
            side_effect=Exception("Coordinator failure")
        )

        with (
            patch(
                "custom_components.idokep_weather_and_forecast.IdokepDataUpdateCoordinator"
            ) as mock_coordinator_class,
            patch(
                "custom_components.idokep_weather_and_forecast.async_get_clientsession"
            ),
            patch("custom_components.idokep_weather_and_forecast.IdokepApiClient"),
            patch("custom_components.idokep_weather_and_forecast.IdokepData"),
        ):
            mock_coordinator_class.return_value = mock_coordinator

            # Call the function and expect exception
            with pytest.raises(Exception, match="Coordinator failure"):
                await async_setup_entry(mock_hass, mock_entry)

    @pytest.mark.asyncio
    async def test_async_unload_entry_success(self) -> None:
        """Test successful unloading of config entry."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.data = {"location": "Budapest"}

        # Mock unload_platforms
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        # Call the function
        result = await async_unload_entry(mock_hass, mock_entry)

        # Assertions
        assert result is True
        mock_hass.config_entries.async_unload_platforms.assert_called_once_with(
            mock_entry, PLATFORMS
        )

    @pytest.mark.asyncio
    async def test_async_unload_entry_failure(self) -> None:
        """Test unloading failure."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.data = {"location": "Budapest"}

        # Mock unload_platforms to return False
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        # Call the function
        result = await async_unload_entry(mock_hass, mock_entry)

        # Assertions
        assert result is False

    @pytest.mark.asyncio
    async def test_async_remove_entry_deletes_saved_data(self) -> None:
        """Removing the entry deletes its file under .storage/."""
        mock_hass = Mock(spec=HomeAssistant)
        mock_entry = Mock(spec=ConfigEntry)
        mock_entry.entry_id = "entry1"

        with patch(
            "custom_components.idokep_weather_and_forecast.Store"
        ) as mock_store_class:
            mock_store_class.return_value.async_remove = AsyncMock()
            await async_remove_entry(mock_hass, mock_entry)

        mock_store_class.assert_called_once_with(
            mock_hass, 1, "idokep_weather_and_forecast.entry1"
        )
        mock_store_class.return_value.async_remove.assert_awaited_once()
