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
    Reading,
    StationReadings,
    latest_readings,
    merge_method,
    merge_readings,
    parse_codes,
    station_codes,
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


def test_latest_readings() -> None:
    """Each series' newest reading; too old ones are left out."""
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

    station = latest_readings(decoded, "jozsamet", NOW)

    assert station.readings == {
        "hom": Reading(11.5, datetime.fromtimestamp(recent, tz=UTC), "jozsamet")
    }
    assert station.available == {"hom", "rh", "p"}
    assert station.last == datetime.fromtimestamp(recent, tz=UTC)


# ---- merging ---------------------------------------------------------------------


def _station(code: str, minutes_ago: int, **values: float) -> StationReadings:
    """Return a station whose readings are minutes_ago old."""
    time = NOW - timedelta(minutes=minutes_ago)
    return StationReadings(
        code,
        {key: Reading(value, time, code) for key, value in values.items()},
        frozenset(values),
        time,
    )


A = _station("a", 10, hom=10.0, rh=80.0, szelirany=350.0)
B = _station("b", 2, hom=12.0, szelirany=10.0, uv=3.0)


def test_merge_priority() -> None:
    """Priority: the first listed station; the next one fills its gaps."""
    data = merge_readings([A, B], "priority")

    assert data.values == {"hom": 10.0, "rh": 80.0, "szelirany": 350.0, "uv": 3.0}
    assert data.sources == {"hom": ("a",), "rh": ("a",), "szelirany": ("a",), "uv": ("b",)}
    assert data.available == {"hom", "rh", "szelirany", "uv"}
    assert data.measured == NOW - timedelta(minutes=2)  # newest reading used (uv)


def test_merge_newest() -> None:
    """Newest: the most recent reading of each series."""
    data = merge_readings([A, B], "newest")

    assert data.values == {"hom": 12.0, "rh": 80.0, "szelirany": 10.0, "uv": 3.0}
    assert data.sources["hom"] == ("b",)
    assert data.sources["rh"] == ("a",)


def test_merge_average() -> None:
    """Average: the mean; wind bearings averaged as angles (350° and 10° → 0°)."""
    data = merge_readings([A, B], "average")

    assert data.values == {"hom": 11.0, "rh": 80.0, "szelirany": 0.0, "uv": 3.0}
    assert data.sources["hom"] == ("a", "b")
    assert data.sources["uv"] == ("b",)


def test_merge_without_fresh_readings() -> None:
    """Reported but too old everywhere: unknown (None), still available."""
    stale = StationReadings("a", {}, frozenset({"hom"}), NOW - timedelta(hours=3))

    data = merge_readings([stale], "priority")

    assert data.values == {"hom": None}
    assert data.sources == {}
    assert data.available == {"hom"}
    assert data.measured == NOW - timedelta(hours=3)


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
async def test_tick_schedule(minute: int, last_ok: bool, fetches: bool) -> None:
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


def _fetching(coordinator: StationCoordinator, files: dict[str, object]) -> object:
    """Patch fetch_station: code -> decoded file, or an exception to raise."""
    coordinator._session = Mock()
    coordinator.codes = list(files)
    coordinator.method = "priority"
    coordinator.logger = Mock()

    async def fetch(_session: object, code: str) -> object:
        result = files[code]
        if isinstance(result, Exception):
            raise result
        return result

    return patch(
        "custom_components.idokep_weather_and_forecast.station.fetch_station", fetch
    )


@pytest.mark.asyncio
async def test_update_failure_raises_update_failed() -> None:
    """If no station can be read, the sensors become unavailable (UpdateFailed)."""
    coordinator = _coordinator()
    down = IdokepApiClientCommunicationError("down")
    with (
        _fetching(coordinator, {"jozsamet": down, "olig13": StationDataError("x")}),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_update_skips_a_failed_station() -> None:
    """A station that fails is left out; the others still give the values."""
    coordinator = _coordinator()
    now = int(NOW.timestamp())
    good = StationSeries(times=[now - 60], series={"hom": [7.5]})
    down = IdokepApiClientCommunicationError("down")
    with (
        _fetching(coordinator, {"jozsamet": down, "ha0kaistvan": good}),
        patch(
            "custom_components.idokep_weather_and_forecast.station.dt_util.utcnow",
            return_value=NOW,
        ),
    ):
        data = await coordinator._async_update_data()

    assert data.values == {"hom": 7.5}
    assert data.sources == {"hom": ("ha0kaistvan",)}


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
    coordinator.codes = ["jozsamet", "olig13"]
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


def test_station_sensor_names_its_stations() -> None:
    """The "stations" attribute lists where the value comes from."""
    data = StationData(
        values={"hom": 11.0, "rh": 80.0},
        sources={"hom": ("jozsamet", "olig13"), "rh": ("olig13",)},
        available=frozenset({"hom", "rh"}),
        measured=NOW,
    )
    hom = next(d for d in STATION_DESCRIPTIONS if d.key == "hom")
    sensor = IdokepStationSensor(_station_mock(data), hom)
    time_sensor = IdokepStationMeasurementTimeSensor(
        _station_mock(data), STATION_MEASUREMENT_TIME_DESCRIPTION
    )

    assert sensor.extra_state_attributes == {"stations": ["jozsamet", "olig13"]}
    assert time_sensor.extra_state_attributes == {"stations": ["jozsamet", "olig13"]}
    no_value = StationData(available=frozenset({"hom"}), values={"hom": None})
    assert IdokepStationSensor(_station_mock(no_value), hom).extra_state_attributes is None


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
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("jozsamet", ["jozsamet"]),  # one code stored as text (older entries)
        ("jozsamet, olig13 jozsamet", ["jozsamet", "olig13"]),
        (["jozsamet", " ", "olig13", "jozsamet"], ["jozsamet", "olig13"]),
    ],
)
def test_parse_codes(raw: object, expected: list[str]) -> None:
    """Codes from a list or a text, in order, without blanks and duplicates."""
    assert parse_codes(raw) == expected


@pytest.mark.parametrize(
    ("data", "options", "codes", "method"),
    [
        ({"location": "X"}, {}, [], "priority"),
        ({"station": "jozsamet"}, {}, ["jozsamet"], "priority"),
        (
            {"station": ["jozsamet"], "merge_method": "newest"},
            {"station": ["olig13", "jozsamet"], "merge_method": "average"},
            ["olig13", "jozsamet"],
            "average",
        ),
        ({"station": ["jozsamet"]}, {"station": [], "merge_method": "x"}, [], "priority"),
    ],
)
def test_station_codes_and_merge_method(
    data: dict, options: dict, codes: list[str], method: str
) -> None:
    """Options override the config flow's values; unknown methods mean priority."""
    entry = Mock(data=data, options=options)
    assert station_codes(entry) == codes
    assert merge_method(entry) == method


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (None, None),
        (StationDataError("bad"), ("station", "jozsamet")),
        (
            IdokepApiClientCommunicationError("down"),
            ("station_connection", "jozsamet"),
        ),
    ],
)
async def test_station_validation(
    side_effect: Exception | None, expected: tuple[str, str] | None
) -> None:
    """The flows check each code by downloading the station's file."""
    with (
        patch.object(config_flow, "async_create_clientsession"),
        patch.object(
            config_flow, "fetch_station", AsyncMock(side_effect=side_effect)
        ) as fetch,
    ):
        assert await config_flow._async_station_error(Mock(), ["jozsamet"]) == expected
        assert await config_flow._async_station_error(Mock(), []) is None
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_station_validation_names_the_bad_code() -> None:
    """With several codes, the error names the first one that fails."""

    async def fetch(_session: object, code: str) -> None:
        if code == "nope":
            raise StationDataError(code)

    with (
        patch.object(config_flow, "async_create_clientsession"),
        patch.object(config_flow, "fetch_station", fetch),
    ):
        problem = await config_flow._async_station_error(
            Mock(), ["jozsamet", "nope", "olig13"]
        )
    assert problem == ("station", "nope")


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
