"""Sensor platform for idokep."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    UV_INDEX,
    UnitOfIrradiance,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolumetricFlux,
)

from .api import WeatherConditionMapper, WindStrengthMapper
from .coordinator import CURRENT, HOURLY
from .entity import IdokepEntity, IdokepStationEntity

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import IdokepDataUpdateCoordinator
    from .data import IdokepConfigEntry
    from .station import StationCoordinator, StationData

# 16-point compass, clockwise from north; states are translated to N, NNE, ...
COMPASS_POINTS = [
    "n",
    "nne",
    "ne",
    "ene",
    "e",
    "ese",
    "se",
    "sse",
    "s",
    "ssw",
    "sw",
    "wsw",
    "w",
    "wnw",
    "nw",
    "nnw",
]
# Sensors whose data comes from the forecast page (/elorejelzes/); the others
# come from the current conditions page (/idojaras/)
HOURLY_KEYS = (
    "temperature_max_today",
    "temperature_min_today",
    "wind_bearing",
    "wind_direction",
    "wind_strength",
    "wind_speed",
    "precipitation_probability",
)


def bearing_to_compass(bearing: float | None) -> str | None:
    """Return the compass point (n, ne, ...) for a bearing in degrees."""
    if bearing is None:
        return None
    # Each point covers 22.5 degrees centred on it: N is 348.75-11.25
    step = 360 / len(COMPASS_POINTS)
    return COMPASS_POINTS[round(bearing / step) % len(COMPASS_POINTS)]


ENTITY_DESCRIPTIONS = (
    SensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        native_unit_of_measurement="°C",
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    # Today's forecast high/low: forecasts, not measurements, so no state_class
    SensorEntityDescription(
        key="temperature_max_today",
        translation_key="temperature_max_today",
        native_unit_of_measurement="°C",
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    SensorEntityDescription(
        key="temperature_min_today",
        translation_key="temperature_min_today",
        native_unit_of_measurement="°C",
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    # Enum so the frontend shows translated names (state.* in translations)
    SensorEntityDescription(
        key="condition",
        translation_key="condition",
        device_class=SensorDeviceClass.ENUM,
        options=WeatherConditionMapper.conditions(),
    ),
    SensorEntityDescription(
        key="wind_bearing",
        translation_key="wind_bearing",
        native_unit_of_measurement=DEGREE,
        device_class=SensorDeviceClass.WIND_DIRECTION,
    ),
    # Same bearing as wind_bearing, shown as a compass point
    SensorEntityDescription(
        key="wind_direction",
        translation_key="wind_direction",
        device_class=SensorDeviceClass.ENUM,
        options=COMPASS_POINTS,
    ),
    # idokep.hu's wind strength word, as a Beaufort level
    SensorEntityDescription(
        key="wind_strength",
        translation_key="wind_strength",
        device_class=SensorDeviceClass.ENUM,
        options=WindStrengthMapper.levels(),
    ),
    # Estimated from the strength (idokep.hu gives no speed); no state_class
    SensorEntityDescription(
        key="wind_speed",
        translation_key="wind_speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    # The current hour's chance of precipitation; 0 when the page shows none
    SensorEntityDescription(
        key="precipitation_probability",
        translation_key="precipitation_probability",
        native_unit_of_measurement=PERCENTAGE,
    ),
)


# The optional weather station's measurements; keys are its series keys
# (see station.SERIES_KEYS). No state_class, like the forecast sensors.
STATION_DESCRIPTIONS = (
    SensorEntityDescription(
        key="hom",
        translation_key="station_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    SensorEntityDescription(
        key="rh",
        translation_key="station_humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    SensorEntityDescription(
        key="p",
        translation_key="station_pressure",
        native_unit_of_measurement=UnitOfPressure.HPA,
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    ),
    SensorEntityDescription(
        key="csap24",
        translation_key="station_precipitation_24h",
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        device_class=SensorDeviceClass.PRECIPITATION,
    ),
    # Resets at midnight
    SensorEntityDescription(
        key="csap23",
        translation_key="station_precipitation_today",
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        device_class=SensorDeviceClass.PRECIPITATION,
    ),
    SensorEntityDescription(
        key="csap1",
        translation_key="station_precipitation_intensity",
        native_unit_of_measurement=UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
    ),
    SensorEntityDescription(
        key="szelero",
        translation_key="station_wind_speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    SensorEntityDescription(
        key="szellokes",
        translation_key="station_wind_gust",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    SensorEntityDescription(
        key="szelirany",
        translation_key="station_wind_bearing",
        native_unit_of_measurement=DEGREE,
        device_class=SensorDeviceClass.WIND_DIRECTION,
    ),
    SensorEntityDescription(
        key="uv",
        translation_key="station_uv_index",
        native_unit_of_measurement=UV_INDEX,
    ),
    SensorEntityDescription(
        key="napfeny",
        translation_key="station_solar_radiation",
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        device_class=SensorDeviceClass.IRRADIANCE,
    ),
    SensorEntityDescription(
        key="vizho",
        translation_key="station_water_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
)
# Time of the station's newest reading
STATION_MEASUREMENT_TIME_DESCRIPTION = SensorEntityDescription(
    key="measurement_time",
    translation_key="station_measurement_time",
    device_class=SensorDeviceClass.TIMESTAMP,
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: IdokepConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    async_add_entities(
        IdokepSensor(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )
    if (station := entry.runtime_data.station) is not None:
        async_add_entities(
            [
                *(
                    IdokepStationSensor(station, entity_description)
                    for entity_description in STATION_DESCRIPTIONS
                ),
                IdokepStationMeasurementTimeSensor(
                    station, STATION_MEASUREMENT_TIME_DESCRIPTION
                ),
            ]
        )


class IdokepSensor(IdokepEntity, SensorEntity):
    """Idokep Sensor class."""

    @property
    def has_entity_name(self) -> bool:
        """Return True if the entity has a name."""
        return True

    def __init__(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor class."""
        source = HOURLY if entity_description.key in HOURLY_KEYS else CURRENT
        super().__init__(coordinator, frozenset({source}))
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def native_value(self) -> str | int | float | None:
        """Return the native value of the sensor."""
        if self.entity_description.key == "wind_direction":
            return bearing_to_compass(self.coordinator.data.get("wind_bearing"))
        value = self.coordinator.data.get(self.entity_description.key)
        options = self.entity_description.options
        if options is not None and value not in options:
            # e.g. "unknown" for an unmapped condition; HA shows it as Unknown
            return None
        return value


class IdokepStationSensor(IdokepStationEntity, SensorEntity):
    """A measurement of the optional weather stations (merged into one value)."""

    def __init__(
        self,
        coordinator: StationCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_station_{entity_description.key}"
        )

    @property
    def available(self) -> bool:
        """Return False if the station doesn't measure this (or isn't reached)."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._reported(self.coordinator.data)
        )

    def _reported(self, data: StationData) -> bool:
        """Return True if any of the stations reports this measurement."""
        return self.entity_description.key in data.available

    def _sources(self, data: StationData) -> tuple[str, ...]:
        """Return the codes of the stations the value comes from."""
        return data.sources.get(self.entity_description.key, ())

    @property
    def native_value(self) -> float | None:
        """Return the merged value (None if no reading from the last hour)."""
        return self.coordinator.data.values.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, list[str]] | None:
        """Return the stations the value comes from."""
        if self.coordinator.data is None:
            return None
        sources = self._sources(self.coordinator.data)
        return {"stations": list(sources)} if sources else None


class IdokepStationMeasurementTimeSensor(IdokepStationSensor):
    """When the newest of the readings shown was taken."""

    def _reported(self, data: StationData) -> bool:
        """Return True once a station has a reading."""
        return data.measured is not None

    def _sources(self, data: StationData) -> tuple[str, ...]:
        """Return every station any shown value comes from."""
        return tuple(dict.fromkeys(c for codes in data.sources.values() for c in codes))

    @property
    def native_value(self) -> datetime | None:
        """Return the newest reading's time."""
        return self.coordinator.data.measured
