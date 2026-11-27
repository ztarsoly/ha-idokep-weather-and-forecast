"""Sensor platform for idokep."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_SUNNY,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    UV_INDEX,
    UnitOfDensity,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolumetricFlux,
)
from homeassistant.helpers import sun, translation
from homeassistant.util import dt as dt_util

from . import calculations
from .api import (
    ALERT_LEVELS,
    WeatherConditionMapper,
    WindStrengthMapper,
    highest_alert_level,
)
from .const import DOMAIN
from .coordinator import SOURCE_ENTITIES
from .entity import IdokepEntity, IdokepStationEntity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import IdokepDataUpdateCoordinator, StationCoordinator
    from .data import IdokepConfigEntry
    from .station import StationData

# Numeric sensors are shown with 1 decimal (users can change it per entity)
DISPLAY_PRECISION = 1

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


# The alert sensors' attribute (a list of the types in force) by sensor key
ALERT_ATTRIBUTES = {"alert_level": "alerts", "warning_level": "warnings"}


def alert_type_label(hass: HomeAssistant, sensor_key: str, alert_type: str) -> str:
    """
    Return an alert type's label in Home Assistant's language.

    The labels are the translations of the attribute's values
    ("state_attributes" of the sensor); the type itself if there's none.
    """
    translations = translation.async_get_cached_translations(
        hass, hass.config.language, "entity", DOMAIN
    )
    attribute = ALERT_ATTRIBUTES[sensor_key]
    key = (
        f"component.{DOMAIN}.entity.sensor.{sensor_key}"
        f".state_attributes.{attribute}.state.{alert_type}"
    )
    return translations.get(key, alert_type)


# Snow sensors: days from today
SNOW_DAYS = {"snow_today": 0, "snow_tomorrow": 1}


def expected_snow(
    daily_forecast: list[dict[str, Any]] | None, day: date
) -> float | None:
    """
    Return the expected snowfall (cm) of a day in the daily forecast.

    0 if the day has no snow; None (the sensor is unavailable) if the day is
    not in the forecast or its precipitation is unknown.
    """
    for item in daily_forecast or []:
        if str(item.get("datetime", ""))[:10] != day.isoformat():
            continue
        if "snow" in item:
            return item["snow"]
        return None if item.get("precipitation") is None else 0
    return None


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
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    # Today's forecast high/low: forecasts, not measurements, so no state_class
    SensorEntityDescription(
        key="temperature_max_today",
        translation_key="temperature_max_today",
        native_unit_of_measurement="°C",
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    SensorEntityDescription(
        key="temperature_min_today",
        translation_key="temperature_min_today",
        native_unit_of_measurement="°C",
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    # Enum so the frontend shows translated names (state.* in translations)
    SensorEntityDescription(
        key="condition",
        translation_key="condition",
        device_class=SensorDeviceClass.ENUM,
        options=WeatherConditionMapper.conditions(),
    ),
    # Estimated from the condition's Hungarian description (0/25/50/75/100 %)
    SensorEntityDescription(
        key="cloud_coverage",
        translation_key="cloud_coverage",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=DISPLAY_PRECISION,
    ),
    SensorEntityDescription(
        key="wind_bearing",
        translation_key="wind_bearing",
        native_unit_of_measurement=DEGREE,
        suggested_display_precision=DISPLAY_PRECISION,
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
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    # The current hour's amount: its share of the day's total (by rain bar)
    # Estimated: 1.75 times the wind speed
    SensorEntityDescription(
        key="wind_gust_speed",
        translation_key="wind_gust_speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    SensorEntityDescription(
        key="precipitation",
        translation_key="precipitation",
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRECIPITATION,
    ),
    # The current hour's chance of precipitation; 0 when the page shows none
    SensorEntityDescription(
        key="precipitation_probability",
        translation_key="precipitation_probability",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=DISPLAY_PRECISION,
    ),
    # Expected snowfall of today and tomorrow (cm), from the daily forecast;
    # the page gives snow in cm, so it isn't converted to the mm of precipitation
    SensorEntityDescription(
        key="snow_today",
        translation_key="snow_today",
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.DISTANCE,
    ),
    SensorEntityDescription(
        key="snow_tomorrow",
        translation_key="snow_tomorrow",
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.DISTANCE,
    ),
    # The highest level among the hourly cards' alerts (the next 3 hours)
    SensorEntityDescription(
        key="alert_level",
        translation_key="alert_level",
        device_class=SensorDeviceClass.ENUM,
        options=ALERT_LEVELS,
    ),
    # The highest level of the banners at the top of the page (alerts or
    # warnings)
    SensorEntityDescription(
        key="warning_level",
        translation_key="warning_level",
        device_class=SensorDeviceClass.ENUM,
        options=ALERT_LEVELS,
    ),
    # Average of the settlement's stations on idokep.hu's maps
    SensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    SensorEntityDescription(
        key="dew_point",
        translation_key="dew_point",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    # Calculated from the temperature, humidity and the estimated wind speed
    SensorEntityDescription(
        key="apparent_temperature",
        translation_key="apparent_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    SensorEntityDescription(
        key="pressure",
        translation_key="pressure",
        native_unit_of_measurement=UnitOfPressure.HPA,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    ),
    SensorEntityDescription(
        key="uv_index",
        translation_key="uv_index",
        native_unit_of_measurement=UV_INDEX,
        suggested_display_precision=DISPLAY_PRECISION,
    ),
)


# The optional weather station's measurements; keys are its series keys
# (see station.SERIES_KEYS). No state_class, like the forecast sensors.
STATION_DESCRIPTIONS = (
    SensorEntityDescription(
        key="hom",
        translation_key="station_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    SensorEntityDescription(
        key="rh",
        translation_key="station_humidity",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    SensorEntityDescription(
        key="p",
        translation_key="station_pressure",
        native_unit_of_measurement=UnitOfPressure.HPA,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    ),
    SensorEntityDescription(
        key="csap24",
        translation_key="station_precipitation_24h",
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRECIPITATION,
    ),
    # Resets at midnight
    SensorEntityDescription(
        key="csap23",
        translation_key="station_precipitation_today",
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRECIPITATION,
    ),
    SensorEntityDescription(
        key="csap1",
        translation_key="station_precipitation_intensity",
        native_unit_of_measurement=UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
    ),
    SensorEntityDescription(
        key="szelero",
        translation_key="station_wind_speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    SensorEntityDescription(
        key="szellokes",
        translation_key="station_wind_gust",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    SensorEntityDescription(
        key="szelirany",
        translation_key="station_wind_bearing",
        native_unit_of_measurement=DEGREE,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.WIND_DIRECTION,
    ),
    SensorEntityDescription(
        key="uv",
        translation_key="station_uv_index",
        native_unit_of_measurement=UV_INDEX,
        suggested_display_precision=DISPLAY_PRECISION,
    ),
    SensorEntityDescription(
        key="napfeny",
        translation_key="station_solar_radiation",
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.IRRADIANCE,
    ),
    SensorEntityDescription(
        key="vizho",
        translation_key="station_water_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
)
# Time of the station's newest reading
STATION_MEASUREMENT_TIME_DESCRIPTION = SensorEntityDescription(
    key="measurement_time",
    translation_key="station_measurement_time",
    device_class=SensorDeviceClass.TIMESTAMP,
)


@dataclass(frozen=True, kw_only=True)
class IdokepCalculatedSensorEntityDescription(SensorEntityDescription):
    """A value calculated from merged station measurements."""

    # Series keys passed to value_fn, in order
    inputs: tuple[str, ...]
    value_fn: Callable[..., float | None]


# Calculated from the merged temperature and humidity (and wind speed)
# pylint doesn't see the fields inherited through HA's FrozenOrThawed metaclass
# pylint: disable=unexpected-keyword-arg
STATION_CALCULATED_DESCRIPTIONS = (
    IdokepCalculatedSensorEntityDescription(
        key="dew_point",
        translation_key="station_dew_point",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
        inputs=("hom", "rh"),
        value_fn=calculations.dew_point,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="apparent_temperature",
        translation_key="station_apparent_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
        inputs=("hom", "rh", "szelero"),
        value_fn=calculations.apparent_temperature,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="wet_bulb_temperature",
        translation_key="station_wet_bulb_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.TEMPERATURE,
        inputs=("hom", "rh"),
        value_fn=calculations.wet_bulb_temperature,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="absolute_humidity",
        translation_key="station_absolute_humidity",
        native_unit_of_measurement=UnitOfDensity.GRAMS_PER_CUBIC_METER,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.ABSOLUTE_HUMIDITY,
        inputs=("hom", "rh"),
        value_fn=calculations.absolute_humidity,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="vapour_pressure",
        translation_key="station_vapour_pressure",
        native_unit_of_measurement=UnitOfPressure.HPA,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRESSURE,
        inputs=("hom", "rh"),
        value_fn=calculations.vapour_pressure,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="vapour_pressure_deficit",
        translation_key="station_vapour_pressure_deficit",
        native_unit_of_measurement=UnitOfPressure.KPA,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.PRESSURE,
        inputs=("hom", "rh"),
        value_fn=calculations.vapour_pressure_deficit,
    ),
    IdokepCalculatedSensorEntityDescription(
        key="cloud_base",
        translation_key="station_cloud_base",
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=DISPLAY_PRECISION,
        device_class=SensorDeviceClass.DISTANCE,
        inputs=("hom", "rh"),
        value_fn=calculations.cloud_base,
    ),
)
# pylint: enable=unexpected-keyword-arg


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: IdokepConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    if (coordinator := entry.runtime_data.coordinator) is not None:
        async_add_entities(
            IdokepSensor(
                coordinator=coordinator,
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
                *(
                    IdokepStationCalculatedSensor(station, entity_description)
                    for entity_description in STATION_CALCULATED_DESCRIPTIONS
                ),
                IdokepStationMeasurementTimeSensor(
                    station, STATION_MEASUREMENT_TIME_DESCRIPTION
                ),
            ]
        )


class IdokepSensor(IdokepEntity, SensorEntity):
    """Idokep Sensor class."""

    def __init__(
        self,
        coordinator: IdokepDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor class."""
        sources = frozenset(
            source
            for source, keys in SOURCE_ENTITIES.items()
            if entity_description.key in keys
        )
        super().__init__(coordinator, sources)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def available(self) -> bool:
        """Return False without a value: unavailable rather than unknown."""
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> str | int | float | None:
        """Return the native value of the sensor."""
        if self.entity_description.key == "wind_direction":
            return bearing_to_compass(self.coordinator.data.get("wind_bearing"))
        if (days := SNOW_DAYS.get(self.entity_description.key)) is not None:
            day = dt_util.now().date() + timedelta(days=days)
            return expected_snow(self.coordinator.data.get("daily_forecast"), day)
        if self.entity_description.key == "alert_level":
            return highest_alert_level(self.coordinator.data.get("alerts"))
        value = self.coordinator.data.get(self.entity_description.key)
        if (
            self.entity_description.key == "condition"
            and value == ATTR_CONDITION_SUNNY
            and not sun.is_up(self.hass)
        ):
            return ATTR_CONDITION_CLEAR_NIGHT
        options = self.entity_description.options
        if options is not None and value not in options:
            # e.g. "unknown" for an unmapped condition: unavailable
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, list[dict[str, str]]] | None:
        """
        Return an alert sensor's alerts or warnings in force.

        Each with its label (in Home Assistant's language), type and level.
        """
        key = self.entity_description.key
        attribute = ALERT_ATTRIBUTES.get(key)
        items = self.coordinator.data.get(attribute) if attribute else None
        if items is None:
            return None
        return {
            attribute: [
                {
                    "label": alert_type_label(self.hass, key, item["type"]),
                    "type": item["type"],
                    "level": item["level"],
                }
                for item in items
            ]
        }


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
        """
        Return False without a value: unavailable rather than unknown.

        No station is read or has ever measured this, or every reading is over
        the age limit.
        """
        return (
            super().available
            and self.coordinator.data is not None
            and self.native_value is not None
        )

    def _sources(self, data: StationData) -> tuple[str, ...]:
        """Return the codes of the stations the value comes from."""
        return data.sources.get(self.entity_description.key, ())

    @property
    def native_value(self) -> float | None:
        """Return the merged value (None if every reading is over the age limit)."""
        return self.coordinator.data.values.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, list[str]] | None:
        """Return the stations the value comes from."""
        if self.coordinator.data is None:
            return None
        sources = self._sources(self.coordinator.data)
        return {"stations": list(sources)} if sources else None


class IdokepStationCalculatedSensor(IdokepStationSensor):
    """A value calculated from the stations' merged measurements."""

    entity_description: IdokepCalculatedSensorEntityDescription

    def _sources(self, data: StationData) -> tuple[str, ...]:
        """Return the stations the inputs come from."""
        return tuple(
            dict.fromkeys(
                code
                for key in self.entity_description.inputs
                for code in data.sources.get(key, ())
            )
        )

    @property
    def native_value(self) -> float | None:
        """Return the value (None without fresh readings of every input)."""
        values = self.coordinator.data.values
        inputs = [values.get(key) for key in self.entity_description.inputs]
        if any(value is None for value in inputs):
            return None
        return self.entity_description.value_fn(*inputs)


class IdokepStationMeasurementTimeSensor(IdokepStationSensor):
    """When the newest of the readings shown was taken."""

    def _sources(self, data: StationData) -> tuple[str, ...]:
        """Return every station any shown value comes from."""
        return tuple(dict.fromkeys(c for codes in data.sources.values() for c in codes))

    @property
    def native_value(self) -> datetime | None:
        """Return the newest reading's time."""
        return self.coordinator.data.measured
