"""Tests for the optional weather station (automata) device."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.idokep_weather_and_forecast import config_flow
from custom_components.idokep_weather_and_forecast.api import (
    IdokepApiClientCommunicationError,
)
from custom_components.idokep_weather_and_forecast.const import DOMAIN
from custom_components.idokep_weather_and_forecast.sensor import (
    STATION_DESCRIPTIONS,
    STATION_MEASUREMENT_TIME_DESCRIPTION,
    IdokepStationMeasurementTimeSensor,
    IdokepStationSensor,
    async_setup_entry,
)
from custom_components.idokep_weather_and_forecast.station import (
    _MASK32,
    _SEED,
    SERIES_KEYS,
    StationCoordinator,
    StationData,
    StationDataError,
    StationSeries,
    decode_station,
    latest_measurements,
    station_code,
)

FIXTURE = Path(__file__).parent / "fixtures" / "station_jozsamet.bin"
NOW = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)


# ---- a test encoder (inverse of the decoder) --------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "little")


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def encode_station(
    times: list[int], series: dict[str, list[float | None]], decimals: int = 1
) -> bytes:
    """Build a station file like idokep.hu's (None = missing reading)."""
    body = bytearray(_u32(times[-1]) + _u32(times[0]) + _u16(len(times)))
    for previous, current in pairwise(times):
        body += _varint(current - previous)
    body.append(0)  # no daylight periods
    body.append(len(series))
    for key, values in series.items():
        body += bytes([SERIES_KEYS.index(key), decimals])
        total = 0
        for value in values:
            if value is None:
                body += _varint(0)
                continue
            scaled = round(value * 10**decimals)
            delta, total = scaled - total, scaled
            zigzag = 2 * delta if delta >= 0 else -2 * delta - 1
            body += _varint(zigzag + 1)

    key = 0x12345678
    state = _SEED ^ key
    previous = key & 0xFF
    out = bytearray([1]) + _u32(key)
    for byte in body:
        state ^= (state << 13) & _MASK32
        state ^= state >> 17
        state ^= (state << 5) & _MASK32
        cipher = byte ^ (state >> 24) ^ previous
        out.append(cipher)
        previous = cipher
    return bytes(out)


# ---- decoding -----------------------------------------------------------------


def test_decode_round_trip() -> None:
    """Values, gaps and negative steps survive encoding and decoding."""
    start = int(NOW.timestamp())
    raw = encode_station(
        [start, start + 60, start + 180],
        {"hom": [12.9, None, -3.4], "szelirany": [338, 0, 15]},
    )
    decoded = decode_station(raw)

    assert decoded.times == [start, start + 60, start + 180]
    assert decoded.series["hom"][0] == 12.9
    assert math.isnan(decoded.series["hom"][1])
    assert decoded.series["hom"][2] == -3.4
    assert decoded.series["szelirany"] == [338, 0, 15]


@pytest.mark.parametrize("raw", [b"", b"\x02" + bytes(20), b"\x01" + bytes(5)])
def test_decode_rejects_unknown_format(raw: bytes) -> None:
    """Wrong version byte or too short: not a station file."""
    with pytest.raises(StationDataError):
        decode_station(raw)


def test_decode_rejects_truncated_file() -> None:
    """A file cut short raises instead of returning partial data."""
    start = int(NOW.timestamp())
    raw = encode_station([start, start + 60], {"hom": [1.0, 2.0]})
    with pytest.raises(StationDataError):
        decode_station(raw[:-2])


def test_decode_real_station_file() -> None:
    """A real file (Debrecen-Józsa, jozsamet) decodes to sensible values."""
    decoded = decode_station(FIXTURE.read_bytes())

    assert len(decoded.times) > 1000  # about a reading a minute for 24 hours
    assert decoded.times == sorted(decoded.times)
    assert set(decoded.series) == {
        "hom",
        "rh",
        "p",
        "csap24",
        "szelero",
        "szellokes",
        "szelirany",
        "csap1",
    }
    for value in decoded.series["rh"]:
        assert 0 <= value <= 100
    for value in decoded.series["p"]:
        assert 950 < value < 1070
    for value in decoded.series["szelirany"]:
        assert 0 <= value <= 360


# ---- latest values -------------------------------------------------------------


def test_latest_measurements() -> None:
    """Each series' newest reading; too old ones become None."""
    old = int((NOW - timedelta(hours=2)).timestamp())
    recent = int((NOW - timedelta(minutes=5)).timestamp())
    decoded = StationSeries(
        times=[old, recent],
        series={
            "hom": [10.0, 11.5],
            "rh": [80.0, math.nan],  # last reading missing: falls back...
            "p": [1020.0, math.nan],  # ...but too old to report
            "uv": [math.nan, math.nan],  # never measured
        },
    )
    rh_only = StationSeries(times=[recent, recent + 60], series={"rh": [70, math.nan]})

    data = latest_measurements(decoded, NOW)

    assert data.values == {"hom": 11.5, "rh": None, "p": None, "uv": None}
    assert data.available == {"hom", "rh", "p"}
    assert data.measured == datetime.fromtimestamp(recent, tz=UTC)
    assert latest_measurements(rh_only, NOW).values == {"rh": 70}


# ---- coordinator ----------------------------------------------------------------


def _coordinator() -> StationCoordinator:
    coordinator = StationCoordinator.__new__(StationCoordinator)
    coordinator.config_entry = Mock(pref_disable_polling=False)
    coordinator._listeners = {"x": Mock()}
    coordinator.last_update_success = True
    coordinator.async_refresh = AsyncMock()
    return coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("minute", "last_ok", "fetches"),
    [(0, True, True), (5, True, True), (3, True, False), (3, False, True)],
)
async def test_tick_schedule(minute: int, last_ok: bool, fetches: bool) -> None:  # noqa: FBT001
    """Fetch on every 5th minute; in between only to retry a failure."""
    coordinator = _coordinator()
    coordinator.last_update_success = last_ok
    await coordinator.async_tick(NOW.replace(minute=minute, second=59))
    assert coordinator.async_refresh.await_count == int(fetches)


@pytest.mark.asyncio
async def test_tick_respects_polling_and_listeners() -> None:
    """No fetch with polling off or with no enabled station sensor."""
    coordinator = _coordinator()
    coordinator.config_entry.pref_disable_polling = True
    await coordinator.async_tick(NOW)
    coordinator.config_entry.pref_disable_polling = False
    coordinator._listeners = {}
    await coordinator.async_tick(NOW)
    coordinator.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_failure_raises_update_failed() -> None:
    """A failed download makes the sensors unavailable (UpdateFailed)."""
    coordinator = _coordinator()
    coordinator._session = Mock()
    coordinator.code = "jozsamet"
    with (
        patch(
            "custom_components.idokep_weather_and_forecast.station.fetch_station",
            AsyncMock(side_effect=IdokepApiClientCommunicationError("down")),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


# ---- sensors ---------------------------------------------------------------------


def test_station_descriptions_cover_all_series() -> None:
    """One sensor per series (12), each with its own name."""
    assert sorted(d.key for d in STATION_DESCRIPTIONS) == sorted(SERIES_KEYS)
    keys = [d.translation_key for d in STATION_DESCRIPTIONS]
    assert len(set(keys)) == len(SERIES_KEYS)
    assert all(key.startswith("station_") for key in keys)


def test_station_description_classes() -> None:
    """Device classes of a few station sensors; none has a state class."""
    by_key = {d.key: d for d in STATION_DESCRIPTIONS}
    assert by_key["hom"].device_class == SensorDeviceClass.TEMPERATURE
    assert by_key["szelirany"].device_class == SensorDeviceClass.WIND_DIRECTION
    assert all(d.state_class is None for d in STATION_DESCRIPTIONS)
    assert by_key["uv"].device_class is None


def _station_mock(data: StationData | None) -> Mock:
    coordinator = Mock(spec=StationCoordinator)
    coordinator.config_entry = Mock(entry_id="entry1", title="Debrecen-Józsa")
    coordinator.code = "jozsamet"
    coordinator.data = data
    coordinator.last_update_success = True
    return coordinator


def test_station_sensor_device_and_value() -> None:
    """The sensor belongs to the "<entry> Weather" device and shows the value."""
    data = StationData(values={"hom": 12.9}, available=frozenset({"hom"}))
    description = next(d for d in STATION_DESCRIPTIONS if d.key == "hom")
    sensor = IdokepStationSensor(_station_mock(data), description)

    assert sensor.unique_id == "entry1_station_hom"
    device = sensor.device_info
    assert device["identifiers"] == {(DOMAIN, "entry1_station")}
    assert device["name"] == "Debrecen-Józsa Weather"
    assert device["model"] == "Current weather"
    assert device["entry_type"] == DeviceEntryType.SERVICE
    assert device["configuration_url"] == "https://www.idokep.hu/automata/jozsamet"
    assert sensor.available
    assert sensor.native_value == 12.9


def test_station_sensor_unavailable_if_not_measured() -> None:
    """A series the station doesn't report is unavailable."""
    data = StationData(values={"hom": 12.9}, available=frozenset({"hom"}))
    description = next(d for d in STATION_DESCRIPTIONS if d.key == "uv")
    assert not IdokepStationSensor(_station_mock(data), description).available
    assert not IdokepStationSensor(_station_mock(None), description).available


@pytest.mark.asyncio
async def test_setup_adds_station_sensors_only_with_station() -> None:
    """The 12 station sensors are added only when a station is configured."""
    entry = Mock()
    entry.runtime_data.coordinator.config_entry.entry_id = "entry1"
    entry.runtime_data.station = _station_mock(None)
    add = Mock()
    await async_setup_entry(Mock(), entry, add)
    station_sensors = list(add.call_args_list[1][0][0])
    # The 12 measurements plus the time of the newest reading
    assert len(station_sensors) == len(SERIES_KEYS) + 1
    assert isinstance(station_sensors[-1], IdokepStationMeasurementTimeSensor)

    entry.runtime_data.station = None
    add = Mock()
    await async_setup_entry(Mock(), entry, add)
    assert add.call_count == 1


# ---- configuration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "options", "expected"),
    [
        ({"location": "X"}, {}, None),
        ({"station": "jozsamet"}, {}, "jozsamet"),
        ({"station": "jozsamet"}, {"station": "olig13"}, "olig13"),
        ({"station": "jozsamet"}, {"station": ""}, None),
    ],
)
def test_station_code(data: dict, options: dict, expected: str | None) -> None:
    """Options override the code given in the config flow; empty = none."""
    assert station_code(Mock(data=data, options=options)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (None, None),
        (StationDataError("bad"), "station"),
        (IdokepApiClientCommunicationError("down"), "station_connection"),
    ],
)
async def test_station_validation(side_effect: Exception | None, expected: str) -> None:
    """The flows check the code by downloading the station's file."""
    with (
        patch.object(config_flow, "async_create_clientsession"),
        patch.object(
            config_flow, "fetch_station", AsyncMock(side_effect=side_effect)
        ) as fetch,
    ):
        assert await config_flow._async_station_error(Mock(), "jozsamet") == expected
        assert await config_flow._async_station_error(Mock(), "") is None
    fetch.assert_awaited_once()


def test_station_measurement_time_sensor() -> None:
    """Measurement time: the newest reading's time, as a timestamp sensor."""
    measured = NOW - timedelta(minutes=2)
    data = StationData(values={}, available=frozenset(), measured=measured)
    sensor = IdokepStationMeasurementTimeSensor(
        _station_mock(data), STATION_MEASUREMENT_TIME_DESCRIPTION
    )

    assert sensor.unique_id == "entry1_station_measurement_time"
    assert sensor.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.available
    assert sensor.native_value == measured
    no_reading = StationData(values={}, available=frozenset(), measured=None)
    assert not IdokepStationMeasurementTimeSensor(
        _station_mock(no_reading), STATION_MEASUREMENT_TIME_DESCRIPTION
    ).available
