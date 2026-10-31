"""
Weather station (automata) measurements for a location entry.

Each idokep.hu weather station has a page (/automata/<code>), but its numbers
are drawn as images there. The page's charts load the same data from
/automata/<code>.bin: about the last 24 hours, roughly one reading a minute,
of up to 12 measurements. That file is decoded here (a port of the site's
own decoder in /js/automataChart.min.js).

The stations are optional: they are set in the config flow (or later in the
entry's options) by their codes, e.g. "jozsamet". With several stations,
each sensor's value is merged from their readings: by priority (the first
listed station with a fresh reading) or the average.
"""

from __future__ import annotations

import asyncio
import math
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    _BROWSER_HEADERS,
    IdokepApiClientCommunicationError,
    IdokepApiClientError,
)
from .const import (
    DOMAIN,
    MERGE_AVERAGE,
    MERGE_METHOD,
    MERGE_METHODS,
    MERGE_PRIORITY,
    STATION,
)

if TYPE_CHECKING:
    from logging import Logger

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

STATION_URL = "https://www.idokep.hu/automata/{code}.bin"
# The station's page on idokep.hu (the device's "Visit" link)
STATION_PAGE_URL = "https://www.idokep.hu/automata/{code}"
TIMEOUT = 10
# Regular fetch on every 5th minute at second 59 (x:00:59, x:05:59, ...);
# after a failure, again every minute until it succeeds
CHECK_EVERY_MINUTES = 5
# A measurement older than this is reported as unknown
MAX_AGE = timedelta(hours=1)

# Series in the order the file numbers them (the site's chart definitions)
SERIES_KEYS = (
    "hom",  # temperature, °C
    "rh",  # relative humidity, %
    "p",  # air pressure, hPa
    "csap24",  # precipitation in the last 24 hours, mm
    "csap23",  # precipitation since midnight, mm
    "szelero",  # wind speed, km/h
    "szellokes",  # wind gust, km/h
    "szelirany",  # wind bearing, degrees
    "csap1",  # precipitation intensity, mm/h
    "uv",  # UV-B index
    "vizho",  # water temperature, °C
    "napfeny",  # global (solar) radiation, W/m²
)

_FORMAT_VERSION = 1
_MIN_LENGTH = 16
_SEED = 1798214099
_SEED_FALLBACK = 2654435769
_MASK32 = 0xFFFFFFFF


def parse_codes(raw: str | Iterable[str] | None) -> list[str]:
    """
    Return the station codes from a list or a text, without duplicates.

    Older entries store one code as text; a text may also list several codes
    separated by commas or spaces.
    """
    if not raw:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    codes: list[str] = []
    for item in items:
        for code in re.split(r"[,\s]+", str(item)):
            if code and code not in codes:
                codes.append(code)
    return codes


def station_codes(entry: ConfigEntry) -> list[str]:
    """Return the entry's weather station codes (options win over data)."""
    return parse_codes(entry.options.get(STATION, entry.data.get(STATION)))


def merge_method(entry: ConfigEntry) -> str:
    """Return the entry's merge method (options win over data)."""
    method = entry.options.get(MERGE_METHOD, entry.data.get(MERGE_METHOD))
    return method if method in MERGE_METHODS else MERGE_PRIORITY


def station_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """Return the station device's identifier (stays when the code changes)."""
    return (DOMAIN, f"{entry.entry_id}_station")


class StationDataError(IdokepApiClientError):
    """The station file is missing or not in the expected format."""


@dataclass
class StationSeries:
    """Decoded station file: reading times (UTC seconds) and series values."""

    times: list[int]
    # Series key -> one value per reading time (NaN where missing)
    series: dict[str, list[float]]


@dataclass(frozen=True)
class Reading:
    """One station's newest value of a series."""

    value: float
    time: datetime
    code: str


@dataclass
class StationReadings:
    """A station's fresh readings."""

    code: str
    # Series key -> newest reading within MAX_AGE
    readings: dict[str, Reading]
    # Series keys the station reports at all
    available: frozenset[str]
    # Time of the newest reading in the file
    last: datetime | None


@dataclass
class StationData:
    """The merged measurements of the entry's stations."""

    # Series key -> merged value (None if no station has a fresh reading)
    values: dict[str, float | None] = field(default_factory=dict)
    # Series key -> codes of the stations the value comes from
    sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Series keys any of the stations reports at all
    available: frozenset[str] = frozenset()
    # Time of the newest reading used
    measured: datetime | None = None


class _Reader:
    """Little-endian integers and varints from the decrypted file body."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def byte(self) -> int:
        if self._pos >= len(self._data):
            msg = "Truncated station file"
            raise StationDataError(msg)
        value = self._data[self._pos]
        self._pos += 1
        return value

    def uint16(self) -> int:
        return self.byte() + 256 * self.byte()

    def uint32(self) -> int:
        return self.uint16() + 65536 * self.uint16()

    def varint(self) -> int:
        value, shift = 0, 1
        while True:
            byte = self.byte()
            value += (byte & 0x7F) * shift
            shift *= 128
            if not byte & 0x80:
                return value


def _decrypt(raw: bytes) -> bytes:
    """Undo the file's xorshift scrambling (byte 0: version, 1-4: key)."""
    key = int.from_bytes(raw[1:5], "little")
    state = (_SEED ^ key) or _SEED_FALLBACK
    previous = key & 0xFF
    out = bytearray(len(raw) - 5)
    for i, byte in enumerate(raw[5:]):
        state ^= (state << 13) & _MASK32
        state ^= state >> 17
        state ^= (state << 5) & _MASK32
        out[i] = byte ^ (state >> 24) ^ previous
        previous = byte
    return bytes(out)


def decode_station(raw: bytes) -> StationSeries:
    """Decode a /automata/<code>.bin file."""
    if len(raw) < _MIN_LENGTH or raw[0] != _FORMAT_VERSION:
        msg = "Unknown station file format"
        raise StationDataError(msg)
    reader = _Reader(_decrypt(raw))

    reader.uint32()  # generation time
    first = reader.uint32()
    count = reader.uint16()
    times = [first]
    for _ in range(1, count):
        times.append(times[-1] + reader.varint())

    for _ in range(reader.byte()):  # daylight periods (for the chart shading)
        reader.uint32()
        reader.uint32()

    series: dict[str, list[float]] = {}
    for _ in range(reader.byte()):
        index = reader.byte()
        scale = 10 ** reader.byte()
        values: list[float] = []
        total = 0
        for _ in range(count):
            delta = reader.varint()
            if delta == 0:
                values.append(math.nan)
                continue
            delta -= 1
            # Zigzag-encoded difference from the previous value
            total += -(delta + 1) // 2 if delta % 2 else delta // 2
            values.append(total / scale)
        if index < len(SERIES_KEYS):
            series[SERIES_KEYS[index]] = values
    return StationSeries(times=times, series=series)


def latest_readings(
    decoded: StationSeries, code: str, now: datetime
) -> StationReadings:
    """Return each series' newest reading, if not older than MAX_AGE."""
    oldest = (now - MAX_AGE).timestamp()
    readings: dict[str, Reading] = {}
    available = set()
    for key, series in decoded.series.items():
        for time, value in zip(reversed(decoded.times), reversed(series), strict=True):
            if math.isnan(value):
                continue
            available.add(key)
            if time >= oldest:
                readings[key] = Reading(
                    value, datetime.fromtimestamp(time, tz=UTC), code
                )
            break
    last = datetime.fromtimestamp(decoded.times[-1], tz=UTC) if decoded.times else None
    return StationReadings(code, readings, frozenset(available), last)


def _average(key: str, readings: list[Reading]) -> float:
    """Return the readings' mean; wind bearings are averaged as angles."""
    if key == "szelirany":
        sin = sum(math.sin(math.radians(r.value)) for r in readings)
        cos = sum(math.cos(math.radians(r.value)) for r in readings)
        return round(math.degrees(math.atan2(sin, cos)), 1) % 360
    return round(sum(r.value for r in readings) / len(readings), 1)


def merge_readings(stations: list[StationReadings], method: str) -> StationData:
    """
    Merge the stations' readings into one value per series.

    stations are in priority order. priority: the first station with a fresh
    reading; average: the mean of all fresh readings.
    """
    available = frozenset().union(*(station.available for station in stations))
    values: dict[str, float | None] = {}
    sources: dict[str, tuple[str, ...]] = {}
    used: list[Reading] = []
    for key in available:
        candidates = [s.readings[key] for s in stations if key in s.readings]
        if not candidates:
            values[key] = None
            continue
        if method == MERGE_AVERAGE:
            chosen = candidates
            values[key] = _average(key, chosen)
        else:
            chosen = candidates[:1]
            values[key] = chosen[0].value
        sources[key] = tuple(reading.code for reading in chosen)
        used.extend(chosen)
    times = [r.time for r in used] or [s.last for s in stations if s.last]
    return StationData(
        values=values,
        sources=sources,
        available=available,
        measured=max(times, default=None),
    )


async def fetch_station(session: aiohttp.ClientSession, code: str) -> StationSeries:
    """
    Download and decode a station's file.

    Raises IdokepApiClientCommunicationError on network/HTTP failure and
    StationDataError if the file can't be decoded (e.g. unknown code).
    """
    url = STATION_URL.format(code=quote(code, safe=""))
    try:
        async with (
            asyncio.timeout(TIMEOUT),
            session.get(url, headers=_BROWSER_HEADERS) as response,
        ):
            response.raise_for_status()
            raw = await response.read()
    except (TimeoutError, aiohttp.ClientError, socket.gaierror) as exception:
        msg = f"Error fetching {url} - {exception}"
        raise IdokepApiClientCommunicationError(msg) from exception
    return decode_station(raw)


class StationCoordinator(DataUpdateCoordinator[StationData]):
    """Keep a location entry's merged weather station measurements up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        config_entry: ConfigEntry,
        session: aiohttp.ClientSession,
        codes: list[str],
        method: str = MERGE_PRIORITY,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name=f"idokep_weather_and_forecast_station_{config_entry.entry_id}",
            update_interval=None,
            config_entry=config_entry,
        )
        self._session = session
        self.codes = codes
        self.method = method

    async def async_tick(self, now: datetime) -> None:
        """
        Fetch on every 5th minute; retry every minute after a failure.

        Called every minute at second 59. Like the other pages: honours
        "Enable polling for updates" and skips while no station sensor is
        enabled.
        """
        if self.config_entry.pref_disable_polling or not self._listeners:
            return
        if now.minute % CHECK_EVERY_MINUTES and self.last_update_success:
            return
        await self.async_refresh()

    async def _async_update_data(self) -> StationData:
        """
        Download every station's file and merge their readings.

        A station that fails is left out this time (the others fill in);
        the update fails only if none of them could be read.
        """
        results = await asyncio.gather(
            *(fetch_station(self._session, code) for code in self.codes),
            return_exceptions=True,
        )
        now = dt_util.utcnow()
        stations: list[StationReadings] = []
        errors: list[IdokepApiClientError] = []
        for code, result in zip(self.codes, results, strict=True):
            if isinstance(result, IdokepApiClientError):
                self.logger.info("Weather station %s not read: %s", code, result)
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                stations.append(latest_readings(result, code, now))
        if not stations:
            raise UpdateFailed(errors[0])
        return merge_readings(stations, self.method)
