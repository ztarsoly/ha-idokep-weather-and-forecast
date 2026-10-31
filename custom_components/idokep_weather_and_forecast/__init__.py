"""
Custom integration to integrate idokep with Home Assistant.

For more details about this integration, please refer to
https://github.com/ludeeus/integration_blueprint
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from .api import IdokepApiClient
from .const import DOMAIN, ENTRY_TYPE, LOGGER, RADAR, RADAR_UNIQUE_ID
from .coordinator import IdokepDataUpdateCoordinator
from .data import IdokepData, RadarData
from .radar import RadarCoordinator
from .station import (
    StationCoordinator,
    merge_method,
    station_codes,
    station_identifier,
)
from .storage import STORAGE_VERSION, storage_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IdokepConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.WEATHER,
]
# The national Radar service entry only has the radar image
RADAR_PLATFORMS: list[Platform] = [Platform.IMAGE]


def _is_radar(entry: IdokepConfigEntry) -> bool:
    """Return True for the Radar service entry."""
    return entry.data.get(ENTRY_TYPE) == RADAR


def _ensure_radar_entry(hass: HomeAssistant) -> None:
    """
    Create the national Radar service entry if there is none.

    Runs whenever a location entry starts (added, reloaded, Home Assistant
    restarted), so a deleted Radar entry comes back. A disabled or ignored
    one counts as existing and is left alone.
    """
    if any(
        entry.unique_id == RADAR_UNIQUE_ID
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT})
    )


def _remove_station_device(hass: HomeAssistant, entry: IdokepConfigEntry) -> None:
    """Remove the weather station device (and its sensors) left from before."""
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={station_identifier(entry)})
    if device is not None:
        registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IdokepConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    if _is_radar(entry):
        return await _async_setup_radar_entry(hass, entry)

    _ensure_radar_entry(hass)

    coordinator = IdokepDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        config_entry=entry,
    )
    entry.runtime_data = IdokepData(
        client=IdokepApiClient(
            session=async_get_clientsession(hass),
        ),
        coordinator=coordinator,
    )

    # Start from the data saved before the restart (if still valid), so the
    # entry also comes up while idokep.hu is unreachable
    await coordinator.async_restore()

    # Ensure data is available before creating entities
    await coordinator.async_config_entry_first_refresh()

    station = None
    if codes := station_codes(entry):
        station = StationCoordinator(
            hass=hass,
            logger=LOGGER,
            config_entry=entry,
            session=async_get_clientsession(hass),
            codes=codes,
            method=merge_method(entry),
        )
        entry.runtime_data.station = station
        # Not a first refresh: an unreachable station must not stop the
        # forecast from setting up (its sensors are unavailable until the
        # next successful fetch)
        await station.async_refresh()
    else:
        _remove_station_device(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Check the per-page schedules at every whole minute (hh:mm:00)
    entry.async_on_unload(
        async_track_time_change(hass, coordinator.async_tick, second=0)
    )
    if station is not None:
        # Station: every minute at second 59 (fetches on x:00:59, x:05:59, ...)
        entry.async_on_unload(
            async_track_time_change(hass, station.async_tick, second=59)
        )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: IdokepConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    platforms = RADAR_PLATFORMS if _is_radar(entry) else PLATFORMS
    return await hass.config_entries.async_unload_platforms(entry, platforms)


async def _async_setup_radar_entry(
    hass: HomeAssistant,
    entry: IdokepConfigEntry,
) -> bool:
    """Set up the national Radar service entry."""
    coordinator = RadarCoordinator(
        hass=hass,
        logger=LOGGER,
        config_entry=entry,
        session=async_get_clientsession(hass),
    )
    entry.runtime_data = RadarData(coordinator=coordinator)

    # Downloads all frames and builds the first animation
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, RADAR_PLATFORMS)
    # Every minute at second 59: checks on x:00:59, x:05:59, ... and retries in
    # between until the new frame arrives (cheap "304" while unchanged)
    entry.async_on_unload(
        async_track_time_change(hass, coordinator.async_tick, second=59)
    )
    return True


async def async_remove_entry(
    hass: HomeAssistant,
    entry: IdokepConfigEntry,
) -> None:
    """Delete the entry's saved data when the integration entry is removed."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
