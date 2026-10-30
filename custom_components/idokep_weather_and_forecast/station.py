"""
Weather station (automata) measurements for a location entry.

Each idokep.hu weather station has a page (/automata/<code>), but its numbers
are drawn as images there. The page's charts load the same data from
/automata/<code>.bin: about the last 24 hours, roughly one reading a minute,
of up to 12 measurements. That file is decoded here (a port of the site's
own decoder in /js/automataChart.min.js).

The station is optional: it is set in the config flow (or later in the
entry's options) by its code, e.g. "jozsamet".
"""

from __future__ import annotations

import asyncio
import math
import socket
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
from .const import DOMAIN, STATION

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


def station_code(entry: ConfigEntry) -> str | None:
    """Return the entry's weather station code (options win over data)."""
    code = entry.options.get(STATION, entry.data.get(STATION))
    return code or None


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


@dataclass
class StationData:
    """The latest measurements of a station."""

    # Series key -> latest value within MAX_AGE (None if older or missing)
    values: dict[str, float | None] = field(default_factory=dict)
    # Series keys the station reports at all
    available: frozenset[str] = frozenset()
    # Time of the newest reading
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


def latest_measurements(decoded: StationSeries, now: datetime) -> StationData:
    """Return each series' newest value, if not older than MAX_AGE."""
    oldest = (now - MAX_AGE).timestamp()
    values: dict[str, float | None] = {}
    available = set()
    for key, series in decoded.series.items():
        values[key] = None
        for time, value in zip(reversed(decoded.times), reversed(series), strict=True):
            if math.isnan(value):
                continue
            available.add(key)
            if time >= oldest:
                values[key] = value
            break
    measured = (
        datetime.fromtimestamp(decoded.times[-1], tz=UTC) if decoded.times else None
    )
    return StationData(values=values, available=frozenset(available), measured=measured)


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
    """Keep a location entry's weather station measurements up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: Logger,
        config_entry: ConfigEntry,
        session: aiohttp.ClientSession,
        code: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=logger,
            name=f"idokep_weather_and_forecast_station_{code}",
            update_interval=None,
            config_entry=config_entry,
        )
        self._session = session
        self.code = code

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
        """Download the station file and pick the newest values."""
        try:
            decoded = await fetch_station(self._session, self.code)
        except IdokepApiClientError as exception:
            raise UpdateFailed(exception) from exception
        return latest_measurements(decoded, dt_util.utcnow())
