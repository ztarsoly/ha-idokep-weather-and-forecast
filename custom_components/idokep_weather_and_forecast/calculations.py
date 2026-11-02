"""Values calculated from the stations' temperature, humidity and wind."""

from __future__ import annotations

import math

# Results are rounded to 2 decimals, like the merged station values
DECIMALS = 2

# Magnus formula constants (Alduchov & Eskridge, 1996), for -40...50 °C
MAGNUS_A = 6.1094  # hPa
MAGNUS_B = 17.625
MAGNUS_C = 243.04  # °C

# 1000 g/kg * 100 Pa/hPa / 461.5 J/(kg·K) (the gas constant of water vapour)
ABSOLUTE_HUMIDITY_FACTOR = 216.7
KELVIN = 273.15
# Height difference per °C of temperature - dew point spread (Espy)
CLOUD_BASE_M_PER_K = 125
KMH_PER_MS = 3.6


def _valid(temperature: float | None, humidity: float | None) -> bool:
    """Return True if both inputs are known and the humidity is above 0%."""
    return temperature is not None and humidity is not None and 0 < humidity <= 100


def _saturation_vapour_pressure(temperature: float) -> float:
    """Return the saturation vapour pressure over water (hPa)."""
    return MAGNUS_A * math.exp(MAGNUS_B * temperature / (MAGNUS_C + temperature))


def _dew_point(temperature: float, humidity: float) -> float:
    """Return the unrounded dew point (°C)."""
    gamma = math.log(humidity / 100) + MAGNUS_B * temperature / (MAGNUS_C + temperature)
    return MAGNUS_C * gamma / (MAGNUS_B - gamma)


def dew_point(temperature: float | None, humidity: float | None) -> float | None:
    """Return the dew point (°C) from temperature (°C) and humidity (%)."""
    if not _valid(temperature, humidity):
        return None
    return round(_dew_point(temperature, humidity), DECIMALS)


def apparent_temperature(
    temperature: float | None, humidity: float | None, wind_speed: float | None
) -> float | None:
    """
    Return the apparent temperature (°C) without solar radiation.

    Steadman's formula as used by the Australian Bureau of Meteorology:
    AT = T + 0.33 e - 0.70 v - 4.00, with the vapour pressure e (hPa) and
    the wind speed v (m/s; given here in km/h).
    """
    if not _valid(temperature, humidity) or wind_speed is None:
        return None
    # The BoM's own vapour pressure formula (hPa)
    actual = humidity / 100 * 6.105 * math.exp(17.27 * temperature / (237.7 + temperature))
    wind = wind_speed / KMH_PER_MS
    return round(temperature + 0.33 * actual - 0.70 * wind - 4.00, DECIMALS)


def wet_bulb_temperature(
    temperature: float | None, humidity: float | None
) -> float | None:
    """
    Return the wet-bulb temperature (°C), Stull's formula (2011).

    Made for sea-level pressure, 5-99% humidity and -20...50 °C; accurate to
    about 0.3 °C there. Never above the air temperature.
    """
    if not _valid(temperature, humidity):
        return None
    t, rh = temperature, humidity
    wet_bulb = (
        t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh**1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )
    return round(min(wet_bulb, t), DECIMALS)


def absolute_humidity(
    temperature: float | None, humidity: float | None
) -> float | None:
    """Return the absolute humidity (g/m³): water vapour per cubic metre of air."""
    if not _valid(temperature, humidity):
        return None
    actual = _saturation_vapour_pressure(temperature) * humidity / 100
    return round(ABSOLUTE_HUMIDITY_FACTOR * actual / (temperature + KELVIN), DECIMALS)


def vapour_pressure(temperature: float | None, humidity: float | None) -> float | None:
    """Return the vapour pressure (hPa): the water vapour's share of the air pressure."""
    if not _valid(temperature, humidity):
        return None
    return round(_saturation_vapour_pressure(temperature) * humidity / 100, DECIMALS)


def vapour_pressure_deficit(
    temperature: float | None, humidity: float | None
) -> float | None:
    """Return the vapour pressure deficit (kPa): how far the air is from saturation."""
    if not _valid(temperature, humidity):
        return None
    saturation = _saturation_vapour_pressure(temperature)
    # hPa -> kPa
    return round(saturation * (1 - humidity / 100) / 10, DECIMALS)


def cloud_base(temperature: float | None, humidity: float | None) -> float | None:
    """
    Return the estimated cloud base (m above the station).

    125 m per °C of temperature - dew point spread; holds for cumulus clouds
    formed by rising air, not for layered clouds.
    """
    if not _valid(temperature, humidity):
        return None
    spread = temperature - _dew_point(temperature, humidity)
    return round(max(spread, 0) * CLOUD_BASE_M_PER_K, DECIMALS)
