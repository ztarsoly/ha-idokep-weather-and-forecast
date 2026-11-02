"""Image platform for idokep: the animated national radar."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers import translation
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .radar import RADAR_PAGE_URL, RadarCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import RadarConfigEntry

RADAR_MODEL = "National radar image"
# Device and entity names: translated ("device" and "entity.image" in the
# translations), the entity's falls back to English
RADAR_TRANSLATION_KEY = "national_radar"
RADAR_ENTITY_NAME = "National radar image"


def radar_entity_name(hass: HomeAssistant) -> str:
    """Return the image's name in Home Assistant's language."""
    translations = translation.async_get_cached_translations(
        hass, hass.config.language, "entity", DOMAIN
    )
    key = f"component.{DOMAIN}.entity.image.{RADAR_TRANSLATION_KEY}.name"
    return translations.get(key, RADAR_ENTITY_NAME)


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
    # Named on its own ("National radar image"), not "<device> <entity>", so
    # HA doesn't translate the name itself (see radar_entity_name)
    _attr_has_entity_name = False
    # Used for the icon in icons.json
    _attr_translation_key = RADAR_TRANSLATION_KEY

    def __init__(self, hass: HomeAssistant, coordinator: RadarCoordinator) -> None:
        """Initialize the radar image."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_national_radar"
        self._attr_name = radar_entity_name(hass)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            translation_key=RADAR_TRANSLATION_KEY,
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
