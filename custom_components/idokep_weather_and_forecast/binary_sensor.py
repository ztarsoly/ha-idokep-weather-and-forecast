"""Binary sensor platform for idokep."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .api import TimeUtils
from .coordinator import HOURLY
from .entity import IdokepEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import IdokepDataUpdateCoordinator
    from .data import IdokepConfigEntry

STORM_CONDITIONS = ("lightning", "lightning-rainy")

ENTITY_DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="storm_expected_1h",
        translation_key="storm_expected_1h",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    BinarySensorEntityDescription(
        key="weather_alert",
        translation_key="weather_alert",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    BinarySensorEntityDescription(
        key="alert_yellow",
        translation_key="alert_yellow",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    BinarySensorEntityDescription(
        key="alert_orange",
        translation_key="alert_orange",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
    BinarySensorEntityDescription(
        key="alert_red",
        translation_key="alert_red",
        device_class=BinarySensorDeviceClass.SAFETY,
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: IdokepConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary_sensor platform."""
    async_add_entities(
        IdokepBinarySensor(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class IdokepBinarySensor(IdokepEntity, BinarySensorEntity):
    """idokep binary_sensor class."""

    @property
    def has_entity_name(self) -> bool:
        """Return True if the entity has a name."""
        return True

    def __init__(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary_sensor class."""
        # Storm and alerts all come from the hourly forecast page
        super().__init__(coordinator, frozenset({HOURLY}))
        self.entity_description = entity_description
        # Set unique_id for entity persistence after restart
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def is_on(self) -> bool:
        """Return true if the binary_sensor is on."""
        check_methods = {
            "storm_expected_1h": self._check_storm_expected_next_hour,
            "weather_alert": self._check_any_alert,
            "alert_yellow": lambda: self._check_alert_level("yellow"),
            "alert_orange": lambda: self._check_alert_level("orange"),
            "alert_red": lambda: self._check_alert_level("red"),
        }

        check_method = check_methods.get(self.entity_description.key)
        return check_method() if check_method else False

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes for alert sensors."""
        if self.entity_description.key == "weather_alert":
            return self._get_all_alerts_attributes()
        if self.entity_description.key in ("alert_yellow", "alert_orange", "alert_red"):
            level = self.entity_description.key.replace("alert_", "")
            return self._get_level_alert_attributes(level)
        return {}

    def _check_storm_expected_next_hour(self) -> bool:
        """Check if storm is expected in the next hour."""
        now = dt.datetime.now(dt.UTC)
        next_hour = now + dt.timedelta(hours=1)

        for forecast in self.coordinator.data.get("hourly_forecast", []):
            # Conditions are already mapped to Home Assistant values
            if forecast.get("condition") in STORM_CONDITIONS:
                try:
                    forecast_dt = dt.datetime.fromisoformat(
                        forecast.get("datetime", "")
                    )
                    # Forecast times are Budapest local time when no offset given
                    if forecast_dt.tzinfo is None:
                        forecast_dt = forecast_dt.replace(
                            tzinfo=TimeUtils.get_local_timezone()
                        )

                    # Each forecast covers a one-hour slot; check it overlaps the
                    # next hour (this includes the slot we are currently in)
                    slot_end = forecast_dt + dt.timedelta(hours=1)
                    if forecast_dt <= next_hour and slot_end > now:
                        return True
                except (ValueError, TypeError):
                    # Skip invalid datetime entries
                    continue
        return False

    def _check_any_alert(self) -> bool:
        """Check if any weather alert is active."""
        alerts = self.coordinator.data.get("alerts", [])
        return len(alerts) > 0

    def _check_alert_level(self, level: str) -> bool:
        """Check if alerts of specific level are active."""
        alerts_by_level = self.coordinator.data.get("alerts_by_level", {})
        level_alerts = alerts_by_level.get(level, [])
        return len(level_alerts) > 0

    def _get_all_alerts_attributes(self) -> dict:
        """Get attributes for all alerts."""
        alerts = self.coordinator.data.get("alerts", [])
        alerts_by_level = self.coordinator.data.get("alerts_by_level", {})

        return {
            "alert_count": len(alerts),
            "yellow_alerts": len(alerts_by_level.get("yellow", [])),
            "orange_alerts": len(alerts_by_level.get("orange", [])),
            "red_alerts": len(alerts_by_level.get("red", [])),
            "alerts": [
                {
                    "level": alert.level,
                    "type": alert.type,
                    "description": alert.description,
                    "icon_url": alert.icon_url,
                }
                for alert in alerts
            ],
        }

    def _get_level_alert_attributes(self, level: str) -> dict:
        """Get attributes for specific alert level."""
        alerts_by_level = self.coordinator.data.get("alerts_by_level", {})
        level_alerts = alerts_by_level.get(level, [])

        return {
            "alert_count": len(level_alerts),
            "alerts": level_alerts,
        }
