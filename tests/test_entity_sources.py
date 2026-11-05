"""Each entity declares the idokep.hu pages it reads (its listener context)."""

from __future__ import annotations

import json
from pathlib import Path
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
from custom_components.idokep_weather_and_forecast.const import DOMAIN
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
    """All entities share one device, named from the entry's name."""
    sensor = IdokepSensor(mock_coordinator, SENSOR_DESCRIPTIONS[0])
    weather = IdokepWeatherEntity(mock_coordinator)
    for device in (sensor.device_info, weather.device_info):
        assert "name" not in device
        assert device["translation_key"] == "forecast"
        assert device["translation_placeholders"] == {"name": "Budapest"}
    assert sensor.device_info["identifiers"] == weather.device_info["identifiers"]


@pytest.mark.parametrize(
    ("language", "forecast", "station"),
    [
        ("en", "Otthon Forecast", "Otthon Weather"),
        ("hu", "Otthon Előrejelzés", "Otthon Időjárás"),
    ],
)
def test_device_name_translations(language: str, forecast: str, station: str) -> None:
    """The translations put the entry's name into the device names."""
    component = Path(__file__).parent.parent / "custom_components" / DOMAIN
    strings = json.loads(
        (component / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )["device"]
    assert strings["forecast"]["name"].format(name="Otthon") == forecast
    assert strings["station"]["name"].format(name="Otthon") == station
