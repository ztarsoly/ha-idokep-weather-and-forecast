"""Tests for the values calculated from station measurements."""

from __future__ import annotations

import pytest

from custom_components.idokep_weather_and_forecast.calculations import (
    absolute_humidity,
    apparent_temperature,
    cloud_base,
    dew_point,
    vapour_pressure,
    vapour_pressure_deficit,
    wet_bulb_temperature,
)

# Temperature (°C), humidity (%); missing or 0% humidity gives no value
INVALID = [(20.0, None), (None, 50.0), (20.0, 0.0), (20.0, 101.0)]


@pytest.mark.parametrize(
    "function",
    [
        dew_point,
        wet_bulb_temperature,
        absolute_humidity,
        vapour_pressure,
        vapour_pressure_deficit,
        cloud_base,
    ],
)
@pytest.mark.parametrize(("temperature", "humidity"), INVALID)
def test_invalid_inputs(
    function: object, temperature: float | None, humidity: float | None
) -> None:
    """No value without temperature or with impossible humidity."""
    assert function(temperature, humidity) is None


@pytest.mark.parametrize(("temperature", "humidity"), INVALID)
def test_apparent_temperature_invalid_inputs(
    temperature: float | None, humidity: float | None
) -> None:
    """No value without temperature, humidity or wind speed."""
    assert apparent_temperature(temperature, humidity, 10.0) is None
    assert apparent_temperature(20.0, 50.0, None) is None


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [
        (20.0, 50.0, 9.26),
        (10.0, 100.0, 10.0),  # saturated: dew point = temperature
        (-5.0, 80.0, -7.92),
        (30.0, 70.0, 23.93),
    ],
)
def test_dew_point(temperature: float, humidity: float, expected: float) -> None:
    """Magnus formula, rounded to 2 decimals."""
    assert dew_point(temperature, humidity) == expected


@pytest.mark.parametrize(
    ("temperature", "humidity", "wind", "expected"),
    [
        (20.0, 50.0, 0.0, 19.85),
        (20.0, 50.0, 18.0, 16.35),  # 18 km/h = 5 m/s: 3.5 °C cooler
        (30.0, 70.0, 10.0, 33.82),  # humid heat feels hotter
        (-5.0, 80.0, 20.0, -11.78),
    ],
)
def test_apparent_temperature(
    temperature: float, humidity: float, wind: float, expected: float
) -> None:
    """Steadman's apparent temperature (BoM), wind given in km/h."""
    assert apparent_temperature(temperature, humidity, wind) == expected


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [
        (20.0, 50.0, 13.7),  # Stull's own example
        (30.0, 70.0, 25.6),
        (25.0, 40.0, 16.38),
        (10.0, 100.0, 9.94),
    ],
)
def test_wet_bulb_temperature(
    temperature: float, humidity: float, expected: float
) -> None:
    """Stull's formula; never above the air temperature."""
    result = wet_bulb_temperature(temperature, humidity)
    assert result == expected
    assert result <= temperature


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [(20.0, 50.0, 8.62), (30.0, 70.0, 21.2), (-5.0, 80.0, 2.73)],
)
def test_absolute_humidity(temperature: float, humidity: float, expected: float) -> None:
    """Grams of water vapour per cubic metre."""
    assert absolute_humidity(temperature, humidity) == expected


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [(20.0, 50.0, 11.67), (10.0, 100.0, 12.26), (-5.0, 80.0, 3.38), (30.0, 70.0, 29.66)],
)
def test_vapour_pressure(temperature: float, humidity: float, expected: float) -> None:
    """The water vapour's share of the air pressure, in hPa."""
    assert vapour_pressure(temperature, humidity) == expected


def test_vapour_pressure_and_deficit_add_up_to_saturation() -> None:
    """Vapour pressure + deficit = saturation vapour pressure (hPa vs kPa)."""
    saturation = vapour_pressure(20.0, 100.0)
    total = vapour_pressure(20.0, 50.0) + vapour_pressure_deficit(20.0, 50.0) * 10
    # The deficit is rounded to 0.01 kPa (0.1 hPa)
    assert total == pytest.approx(saturation, abs=0.06)


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [(20.0, 50.0, 1.17), (25.0, 40.0, 1.9), (10.0, 100.0, 0.0)],
)
def test_vapour_pressure_deficit(
    temperature: float, humidity: float, expected: float
) -> None:
    """Saturation minus actual vapour pressure, in kPa; 0 when saturated."""
    assert vapour_pressure_deficit(temperature, humidity) == expected


@pytest.mark.parametrize(
    ("temperature", "humidity", "expected"),
    [(20.0, 50.0, 1342.36), (30.0, 70.0, 758.69), (10.0, 100.0, 0.0)],
)
def test_cloud_base(temperature: float, humidity: float, expected: float) -> None:
    """125 m per °C of temperature - dew point spread; 0 m when saturated (fog)."""
    assert cloud_base(temperature, humidity) == expected
