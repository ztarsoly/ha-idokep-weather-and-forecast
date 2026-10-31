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
from custom_components.idokep_weather_and_forecast.coordinator import CURRENT, HOURLY
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
    "precipitation_probability": HOURLY,
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
    """The weather entity needs both pages."""
    entity = IdokepWeatherEntity(mock_coordinator)
    assert entity.coordinator_context == frozenset({CURRENT, HOURLY})


def test_device_named_after_entry(mock_coordinator: Mock) -> None:
    """All entities share one device named "<entry name> Forecast"."""
    sensor = IdokepSensor(mock_coordinator, SENSOR_DESCRIPTIONS[0])
    weather = IdokepWeatherEntity(mock_coordinator)
    assert sensor.device_info["name"] == "Budapest Forecast"
    assert weather.device_info["name"] == "Budapest Forecast"
    assert sensor.device_info["identifiers"] == weather.device_info["identifiers"]
