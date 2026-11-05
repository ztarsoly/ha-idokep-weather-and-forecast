"""IdokepEntity class."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IdokepConfig
from .const import FORECAST_DEVICE, LOCATION, MANUFACTURER, STATION_DEVICE
from .coordinator import IdokepDataUpdateCoordinator
from .station import STATION_PAGE_URL, StationCoordinator, station_identifier

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def device_name_fields(config_entry: ConfigEntry, device: str) -> dict[str, Any]:
    """
    Return the device info fields of a translated device name.

    Home Assistant builds the name from the "device" translations with the
    entry's name in it, e.g. "{name} Forecast" (FORECAST_DEVICE, STATION_DEVICE).
    """
    return {
        "translation_key": device,
        "translation_placeholders": {"name": config_entry.title},
    }


def forecast_page_url(config_entry: ConfigEntry) -> str:
    """Return the settlement's idokep.hu page (the Forecast device's link)."""
    location = quote(config_entry.data.get(LOCATION, ""), safe="")
    return IdokepConfig.get_current_weather_url(location)


class IdokepEntity(CoordinatorEntity[IdokepDataUpdateCoordinator]):
    """IdokepEntity class."""

    def __init__(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        sources: frozenset[str],
    ) -> None:
        """
        Initialize.

        sources are the idokep.hu pages (coordinator CURRENT/HOURLY) this
        entity reads. They are registered as the listener context, so the
        coordinator only fetches pages that an enabled entity uses.
        """
        super().__init__(coordinator, context=sources)
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    coordinator.config_entry.domain,
                    coordinator.config_entry.entry_id,
                ),
            },
            **device_name_fields(coordinator.config_entry, FORECAST_DEVICE),
            configuration_url=forecast_page_url(coordinator.config_entry),
        )


class IdokepStationEntity(CoordinatorEntity[StationCoordinator]):
    """An entity of the optional weather station device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StationCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            identifiers={station_identifier(entry)},
            **device_name_fields(entry, STATION_DEVICE),
            manufacturer=MANUFACTURER,
            model="Current weather",
            entry_type=DeviceEntryType.SERVICE,
            # The first (highest priority) station's page
            configuration_url=STATION_PAGE_URL.format(code=coordinator.codes[0]),
        )
