"""Each entity declares the idokep.hu pages it reads (its listener context)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.idokep_weather_and_forecast.binary_sensor import (
    ENTITY_DESCRIPTIONS as BINARY_SENSOR_DESCRIPTIONS,
)
from custom_components.idokep_weather_and_forecast.binary_sensor import (
    IdokepBinarySensor,
)
from custom_components.idokep_weather_and_forecast.coordinator import (
    CURRENT,
    HOURLY,
    HUMIDITY_MAP,
    PRESSURE_MAP,
    SOURCE_ENTITIES,
    UV_MAP,
)
from custom_components.idokep_weather_and_forecast.sensor import (
    ENTITY_DESCRIPTIONS as SENSOR_DESCRIPTIONS,
)
from custom_components.idokep_weather_and_forecast.sensor import IdokepSensor
from custom_components.idokep_weather_and_forecast.weather import IdokepWeatherEntity

if TYPE_CHECKING:
    from unittest.mock import Mock

# Which page each sensor's data comes from
SENSOR_SOURCES = {
    "temperature": CURRENT,
    "temperature_max_today": HOURLY,
    "temperature_min_today": HOURLY,
    "condition": CURRENT,
    "wind_bearing": HOURLY,
    "wind_direction": HOURLY,
    "wind_strength": HOURLY,
    "wind_speed": HOURLY,
    "precipitation": HOURLY,
    "precipitation_probability": HOURLY,
    "alert_level": HOURLY,
    "humidity": HUMIDITY_MAP,
    "dew_point": HUMIDITY_MAP,
    "pressure": PRESSURE_MAP,
    "uv_index": UV_MAP,
}


@pytest.mark.parametrize("description", SENSOR_DESCRIPTIONS, ids=lambda d: d.key)
def test_sensor_sources(mock_coordinator: Mock, description: object) -> None:
    """Sensors listen for the page their value is parsed from."""
    sensor = IdokepSensor(mock_coordinator, description)
    assert sensor.coordinator_context == frozenset({SENSOR_SOURCES[description.key]})


@pytest.mark.parametrize("description", BINARY_SENSOR_DESCRIPTIONS, ids=lambda d: d.key)
def test_binary_sensor_sources(mock_coordinator: Mock, description: object) -> None:
    """Storm and alert sensors all read the hourly forecast page."""
    binary_sensor = IdokepBinarySensor(mock_coordinator, description)
    assert binary_sensor.coordinator_context == frozenset({HOURLY})


def test_weather_sources(mock_coordinator: Mock) -> None:
    """The weather entity keeps two pages going; the maps follow their sensors."""
    entity = IdokepWeatherEntity(mock_coordinator)
    assert entity.coordinator_context == frozenset({CURRENT, HOURLY})


def test_startup_list_matches_the_entities(mock_coordinator: Mock) -> None:
    """SOURCE_ENTITIES names exactly the entities that read each page."""
    entities = [
        IdokepWeatherEntity(mock_coordinator),
        *(IdokepSensor(mock_coordinator, d) for d in SENSOR_DESCRIPTIONS),
        *(IdokepBinarySensor(mock_coordinator, d) for d in BINARY_SENSOR_DESCRIPTIONS),
    ]
    prefix = f"{mock_coordinator.config_entry.entry_id}_"
    readers: dict[str, set[str]] = {}
    for entity in entities:
        for source in entity.coordinator_context:
            readers.setdefault(source, set()).add(entity.unique_id.removeprefix(prefix))

    assert readers.pop(CURRENT)  # always fetched at startup, so not listed
    assert {s: set(keys) for s, keys in SOURCE_ENTITIES.items()} == readers


def test_device_named_after_entry(mock_coordinator: Mock) -> None:
    """All entities share one device named "<entry name> Forecast"."""
    sensor = IdokepSensor(mock_coordinator, SENSOR_DESCRIPTIONS[0])
    weather = IdokepWeatherEntity(mock_coordinator)
    assert sensor.device_info["name"] == "Budapest Forecast"
    assert weather.device_info["name"] == "Budapest Forecast"
    assert sensor.device_info["identifiers"] == weather.device_info["identifiers"]
