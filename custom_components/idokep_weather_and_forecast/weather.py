"""Időkép Weather Entity for Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_SUNNY,
    Forecast,
    WeatherEntity,
)
from homeassistant.components.weather.const import WeatherEntityFeature
from homeassistant.const import UnitOfSpeed
from homeassistant.core import callback
from homeassistant.helpers import sun

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import CURRENT, HOURLY
from .entity import IdokepEntity, device_name, forecast_page_url

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import IdokepDataUpdateCoordinator
    from .data import IdokepConfigEntry

_LOGGER = logging.getLogger(__name__)


class IdokepWeatherEntity(IdokepEntity, WeatherEntity):
    """Idokep Weather entity."""

    # The only entity with an attribution
    _attr_attribution = ATTRIBUTION

    @property
    def device_info(self) -> dict:
        """Return device information for the integration."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.config_entry.entry_id)},
            "name": device_name(self.coordinator.config_entry),
            "manufacturer": MANUFACTURER,
            "model": "Weather forecast",
            "entry_type": "service",
            "configuration_url": forecast_page_url(self.coordinator.config_entry),
        }

    @property
    def has_entity_name(self) -> bool:
        """Return True if the entity has a name."""
        return True

    def __init__(self, coordinator: IdokepDataUpdateCoordinator) -> None:
        """Initialize the IdokepWeatherEntity with the given coordinator."""
        # Current conditions; forecast page for hourly/daily forecast and wind
        super().__init__(coordinator, frozenset({CURRENT, HOURLY}))
        # Main entity of the device: named just like the device
        # ("<name> Forecast"). The unique ID doesn't contain the settlement,
        # so it stays when the settlement is changed in Configure.
        self._attr_name = None
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_weather"
        self._attr_supported_features = (
            WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
        )
        _LOGGER.debug(
            "Initialized weather entity with unique_id: %s", self._attr_unique_id
        )

    @property
    def condition(self) -> str | None:
        """Return the current weather condition."""
        condition = self.coordinator.data.get("condition")
        if condition == ATTR_CONDITION_SUNNY and not sun.is_up(self.hass):
            return ATTR_CONDITION_CLEAR_NIGHT
        return condition

    @property
    def native_temperature(self) -> float | None:
        """Return the current temperature in native units."""
        temp = self.coordinator.data.get("temperature")
        return float(temp) if temp is not None else None

    @property
    def wind_bearing(self) -> float | None:
        """Return the wind bearing for the current hour."""
        return self.coordinator.data.get("wind_bearing")

    @property
    def native_wind_speed(self) -> float | None:
        """Return the estimated wind speed for the current hour (km/h)."""
        return self.coordinator.data.get("wind_speed")

    @property
    def native_wind_speed_unit(self) -> str:
        """Return the unit of the (estimated) wind speed."""
        return UnitOfSpeed.KILOMETERS_PER_HOUR

    @property
    def native_temperature_unit(self) -> str:
        """Return the unit of measurement for temperature."""
        return "°C"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state and push fresh forecasts to forecast subscribers."""
        super()._handle_coordinator_update()
        self.coordinator.config_entry.async_create_task(
            self.hass, self.async_update_listeners(None)
        )

    async def async_forecast_hourly(self) -> list[Forecast]:
        """Return the hourly forecast."""
        return self.coordinator.data.get("hourly_forecast", [])

    async def async_forecast_daily(self) -> list[Forecast]:
        """Return the daily forecast."""
        return self.coordinator.data.get("daily_forecast", [])


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: IdokepConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the weather entity."""
    weather_entity = IdokepWeatherEntity(entry.runtime_data.coordinator)
    async_add_entities([weather_entity])
