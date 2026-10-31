"""Image platform for idokep: the animated national radar."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .radar import RADAR_PAGE_URL, RadarCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import RadarConfigEntry

RADAR_DEVICE_NAME = "National Radar Image"
RADAR_MODEL = "National radar image"
RADAR_ENTITY_NAME = "National radar image"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RadarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the radar image."""
    async_add_entities([IdokepRadarImage(hass, entry.runtime_data.coordinator)])


class IdokepRadarImage(CoordinatorEntity[RadarCoordinator], ImageEntity):
    """Animated GIF of the national radar (last five frames)."""

    _attr_content_type = "image/gif"
    # Named on its own ("National radar image"), not "<device> <entity>"
    _attr_has_entity_name = False
    _attr_name = RADAR_ENTITY_NAME
    # Used for the icon in icons.json
    _attr_translation_key = "national_radar"

    def __init__(self, hass: HomeAssistant, coordinator: RadarCoordinator) -> None:
        """Initialize the radar image."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_national_radar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=RADAR_DEVICE_NAME,
            manufacturer=MANUFACTURER,
            model=RADAR_MODEL,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=RADAR_PAGE_URL,
        )
        self._update_from_coordinator()

    @callback
    def _update_from_coordinator(self) -> None:
        """Take the animation's time from the coordinator."""
        if self.coordinator.data is not None:
            self._attr_image_last_updated = self.coordinator.data.updated

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a new animation."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the animated GIF."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.gif
