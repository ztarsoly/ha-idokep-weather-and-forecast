"""Refactored Időkép API Client with improved separation of concerns."""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import datetime as dt
import html
import re
import socket
import unicodedata
import zoneinfo
from abc import ABC, abstractmethod
from collections import Counter
from operator import itemgetter
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
from slugify import slugify

from .const import LOGGER
from .failures import FailureRecords
from .html_tree import Element, parse_html
from .rounding import round_to, round_whole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# Configuration and constants


class IdokepConfig:
    """Configuration for Időkép API."""

    BASE_URL = "https://www.idokep.hu"
    TIMEOUT = 5

    @classmethod
    def get_current_weather_url(cls, location: str) -> str:
        """Get current weather URL for location."""
        return f"{cls.BASE_URL}/idojaras/{location}"

    @classmethod
    def get_hourly_forecast_url(cls, location: str) -> str:
        """Get hourly forecast URL for location."""
        return f"{cls.BASE_URL}/elorejelzes/{location}"

    @classmethod
    def get_humidity_url(cls) -> str:
        """Get the humidity and dew point maps' URL (all stations)."""
        return f"{cls.BASE_URL}/para"

    @classmethod
    def get_pressure_url(cls) -> str:
        """Get the pressure map's URL (all stations)."""
        return f"{cls.BASE_URL}/legnyomas"

    @classmethod
    def get_uv_url(cls) -> str:
        """Get the UV map's URL (all stations)."""
        return f"{cls.BASE_URL}/uv"


# Exception classes remain the same
class IdokepApiClientError(Exception):
    """Exception to indicate a general API error."""


class IdokepApiClientCommunicationError(IdokepApiClientError):
    """Exception to indicate a communication error."""


class IdokepApiClientUnknownLocationError(IdokepApiClientError):
    """Exception to indicate a settlement idokep.hu doesn't know."""


# What follows the settlement's name in the pages' titles
CURRENT_TITLE_END = " időjárás - Időkép"
FORECAST_TITLE_END = " időjárás előrejelzés | Időkép"


def _titled_settlement(page: Element, ending: str) -> str | None:
    """Return what a page's title names before the ending, None without it."""
    element = page.find("title")
    title = element.text.strip() if element is not None else ""
    return title.removesuffix(ending) if title.endswith(ending) else None


def _require_settlement(served: str, location: str) -> None:
    """
    Raise IdokepApiClientUnknownLocationError for another settlement's page.

    For a name it doesn't know, the site serves Budapest's pages. Letter case
    and accents don't matter.
    """
    if slugify(served) != slugify(location):
        msg = f"idokep.hu served {served} for {location}"
        raise IdokepApiClientUnknownLocationError(msg)


# Weather condition mapper
class WeatherConditionMapper:
    """Maps Hungarian weather conditions to Home Assistant standards."""

    _CONDITION_MAPPING: ClassVar[dict[str, str]] = {
        "napos": "sunny",
        "derült": "sunny",
        "borult": "cloudy",
        "erősen felhős": "cloudy",
        "közepesen felhős": "partlycloudy",
        "gyengén felhős": "partlycloudy",
        "száraz zivatar": "lightning",
        "villámlás": "lightning",
        "zivatar": "lightning-rainy",
        "zápor": "rainy",
        "szitálás": "rainy",
        "gyenge eső": "rainy",
        "eső": "rainy",
        "eső viharos széllel": "rainy",
        "köd": "fog",
        "ködös": "fog",
        "ködszitálás": "fog",
        "párás": "fog",
        "pára": "fog",
        "porvihar": "exceptional",
        "erős eső": "pouring",
        "intenzív eső": "pouring",
        "jégeső": "hail",
        "havazás": "snowy",
        "intenzív havazás": "snowy",
        "hószállingózás": "snowy",
        "hófúvás": "snowy",
        "hófúvás havazással": "snowy",
        "hózápor": "snowy",
        "hózivatar": "snowy",
        "havas eső": "snowy-rainy",
        "fagyott eső": "snowy-rainy",
        "ónos eső": "snowy-rainy",
        "szeles": "windy",
    }

    # Estimated cloud coverage (%) of each description. The cloud words follow
    # the eighths of the sky (derült 0-1/8 ... borult 8/8); continuous rain,
    # snow and fog mean an overcast sky, showers and storms broken clouds;
    # 50 where the text says nothing about the clouds.
    _CLOUD_COVERAGE: ClassVar[dict[str, int]] = {
        "napos": 0,
        "derült": 0,
        "gyengén felhős": 25,
        "közepesen felhős": 50,
        "erősen felhős": 75,
        "borult": 100,
        "szitálás": 100,
        "gyenge eső": 100,
        "eső": 100,
        "erős eső": 100,
        "intenzív eső": 100,
        "eső viharos széllel": 100,
        "zápor": 75,
        "zivatar": 75,
        "hózivatar": 75,
        "száraz zivatar": 75,
        "villámlás": 75,
        "jégeső": 75,
        "hószállingózás": 100,
        "havazás": 100,
        "intenzív havazás": 100,
        "hófúvás havazással": 100,
        "havas eső": 100,
        "fagyott eső": 100,
        "ónos eső": 100,
        "hózápor": 75,
        "hófúvás": 50,
        "köd": 100,
        "ködös": 100,
        "ködszitálás": 100,
        "párás": 50,
        "pára": 50,
        "szeles": 50,
        "porvihar": 50,
    }

    @classmethod
    def cloud_coverage(cls, condition: str | None) -> int | None:
        """Return the estimated cloud coverage (%) of a Hungarian description."""
        if condition is None:
            return None
        return cls._CLOUD_COVERAGE.get(condition.strip().lower())

    @classmethod
    def map_condition(
        cls, condition: str, records: FailureRecords | None = None
    ) -> str:
        """
        Map Hungarian condition to Home Assistant standard condition.

        "unknown" for a description it does not recognise, which is
        warned about once; records keeps what was warned about.
        """
        mapped = cls._CONDITION_MAPPING.get(condition.lower())
        if mapped is None:
            (records or FailureRecords()).warn_once(
                LOGGER, "Unknown idokep.hu weather %r", condition
            )
            return "unknown"
        return mapped

    @classmethod
    def conditions(cls) -> list[str]:
        """Return every condition an entity can show (except unknown)."""
        return sorted({*cls._CONDITION_MAPPING.values(), "clear-night"})


# Condition texts of a storm: those mapped to a lightning condition, hail
# (only falls from thunderclouds), and two with storm in their name that map
# to other conditions
STORM_TEXTS = frozenset(
    {
        "száraz zivatar",
        "villámlás",
        "zivatar",
        "jégeső",
        "eső viharos széllel",
        "porvihar",
    }
)


GUST_FACTOR = 1.75


class WindStrengthMapper:
    """
    Maps idokep.hu wind strength words to Beaufort-scale levels.

    idokep.hu gives no wind speed, only a strength word ("Mérsékelt északi
    szél", class "mersekelt"). The estimated wind speed of a level is the lower
    end of its Beaufort range in km/h, the estimated gust is that speed times
    1.75. Only "gyenge",
    "mersekelt" and "elenk" have been seen on the site so far; the other words
    follow the Hungarian Beaufort names.
    """

    # Level code: lower end of its range in km/h, calm to hurricane (Beaufort 0-12)
    SPEEDS: ClassVar[dict[str, int]] = {
        "calm": 0,
        "light_air": 1,
        "light_breeze": 6,
        "gentle_breeze": 12,
        "moderate_breeze": 20,
        "fresh_breeze": 29,
        "strong_breeze": 39,
        "near_gale": 50,
        "gale": 62,
        "strong_gale": 75,
        "storm": 89,
        "violent_storm": 103,
        "hurricane": 118,
    }
    # Level code: estimated gust in km/h, GUST_FACTOR times the wind speed
    GUSTS: ClassVar[dict[str, float]] = {
        level: round_to(speed * GUST_FACTOR, 2) for level, speed in SPEEDS.items()
    }

    # Accent-free, space-free Hungarian words: level code. The official
    # Hungarian Beaufort names, plus common alternatives
    _WORDS: ClassVar[dict[str, str]] = {
        "szelcsend": "calm",
        "aligerezhetoszel": "light_air",
        "aligerezheto": "light_air",
        "gyengeszello": "light_air",
        "konnyuszel": "light_breeze",
        "konnyu": "light_breeze",
        "enyheszel": "light_breeze",
        "enyhe": "light_breeze",
        "gyengeszel": "gentle_breeze",
        "gyenge": "gentle_breeze",
        "mersekelt": "moderate_breeze",
        "elenk": "fresh_breeze",
        "erosszel": "strong_breeze",
        "eros": "strong_breeze",
        "igeneros": "near_gale",
        "viharosszel": "gale",
        "viharos": "gale",
        "hevesvihar": "strong_gale",
        "erosvihar": "strong_gale",
        "duhongovihar": "storm",
        "duhongo": "storm",
        "teljesvihar": "storm",
        "hevesszelvihar": "violent_storm",
        "orkanszeruvihar": "violent_storm",
        "orkanszeru": "violent_storm",
        "orkan": "hurricane",
    }

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase and drop accents and anything but letters."""
        decomposed = unicodedata.normalize("NFKD", text.lower())
        return "".join(c for c in decomposed if c.isascii() and c.isalpha())

    @classmethod
    def from_class(cls, css_class: str) -> str | None:
        """Return the level for a CSS class such as "mersekelt"."""
        return cls._WORDS.get(cls._normalize(css_class))

    @classmethod
    def from_text(cls, text: str) -> str | None:
        """Return the level for a tooltip such as "Mérsékelt északi szél"."""
        normalized = cls._normalize(text)
        # Longest words first, so "eros vihar" isn't read as "eros"
        for word in sorted(cls._WORDS, key=len, reverse=True):
            if normalized.startswith(word):
                return cls._WORDS[word]
        return None

    @classmethod
    def levels(cls) -> list[str]:
        """Return every level code, calm to hurricane."""
        return list(cls.SPEEDS)


# Time utilities
class TimeUtils:
    """Utilities for time handling."""

    @staticmethod
    def get_local_timezone() -> dt.tzinfo:
        """Get Budapest timezone."""
        return zoneinfo.ZoneInfo("Europe/Budapest")

    @staticmethod
    def day_start(day: dt.date) -> str:
        """
        Return a day's local midnight as ISO text (a daily item's datetime).

        e.g. "2026-11-19T00:00:00+01:00"; the offset follows summer time.
        """
        return dt.datetime.combine(
            day, dt.time(0), tzinfo=TimeUtils.get_local_timezone()
        ).isoformat()


# HTTP client wrapper
# Headers that mimic a real browser to avoid bot-detection 403s
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
}


class HttpClient:
    """HTTP client with error handling."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize HTTP client."""
        self._session = session

    async def get_html(self, url: str) -> str:
        """Get HTML content from URL with error handling."""
        async with self._response(url) as response:
            return await response.text()

    async def check(self, url: str) -> None:
        """Raise get_html's errors if URL is not served; read nothing."""
        async with self._response(url):
            pass

    @contextlib.asynccontextmanager
    async def _response(self, url: str) -> AsyncIterator[aiohttp.ClientResponse]:
        """Request URL; turn network and HTTP failures into the client's error."""
        try:
            async with (
                asyncio.timeout(IdokepConfig.TIMEOUT),
                self._session.get(url, headers=_BROWSER_HEADERS) as response,
            ):
                response.raise_for_status()
                yield response
        except TimeoutError as exception:
            msg = f"Timeout error fetching {url} - {exception}"
            raise IdokepApiClientCommunicationError(msg) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            msg = f"Error fetching {url} - {exception}"
            raise IdokepApiClientCommunicationError(msg) from exception


# Abstract base for parsers
class WeatherParser(ABC):
    """Abstract base class for weather data parsers."""

    def __init__(self, records: FailureRecords | None = None) -> None:
        """
        Initialize the parser.

        records: what is logged already, so unknown words are warned about
        once.
        """
        self._records = records or FailureRecords()

    @abstractmethod
    def parse(self, page: Element) -> dict[str, Any]:
        """Parse weather data from a page."""


# Specific parser implementations
class CurrentWeatherParser(WeatherParser):
    """Parser for current weather data."""

    def parse(self, page: Element) -> dict[str, Any]:
        """Parse current weather data."""
        result = {}

        # Temperature
        temp_div = page.find("div", "current-temperature")
        if temp_div is not None:
            # Match both ASCII 'C' and the Unicode DEGREE CELSIUS sign '\u2103'
            match = re.search(r"(-?\d+)[^\d]*(?:C|\u2103)", temp_div.text)
            if match:
                result["temperature"] = int(match.group(1))

        # Weather condition (class has no 'ik' prefix since 2026 redesign)
        cond_div = page.find("div", "current-weather")
        if cond_div is not None:
            condition = cond_div.text.strip()
            result["condition"] = WeatherConditionMapper.map_condition(
                condition, self._records
            )
            result["cloud_coverage"] = WeatherConditionMapper.cloud_coverage(condition)
            result["storm_now"] = condition.lower() in STORM_TEXTS

        return result


# Alert levels, lowest first ("none": no alert)
ALERT_NONE = "none"
ALERT_LEVELS = [ALERT_NONE, "yellow", "orange", "red"]

# Alert types: idokep.hu's own type names (as in its /api/current_weather
# data) in English, in its order; "other" for a text no type matches
ALERT_TYPES = [
    "thunderstorm",  # zivatar
    "wind",  # szel
    "rain",  # eso
    "freezing_rain",  # onoseso
    "blowing_snow",  # hofuvas
    "snow",  # ho
    "fog",  # kod
    "frost",  # fagy
    "heat",  # meleg
    "other",
]


class AlertParser(WeatherParser):
    """Parser for weather alerts."""

    # Every hourly card has one alert block (empty without an alert). The page
    # shows an alert's icon on its first three cards: the current hour and the
    # next two at the integration's fetch on the hour.
    HOURS = 3

    # Words of an alert text (lower case) and their type, checked in order:
    # "ónos eső" before "eső", "hófúvás" and "hóvihar" before "hó" and "vihar"
    _TYPE_WORDS: ClassVar[list[tuple[tuple[str, ...], str]]] = [
        (("ónos",), "freezing_rain"),
        (("hófúvás", "hóvihar"), "blowing_snow"),
        (("zivatar",), "thunderstorm"),
        (("szél", "vihar"), "wind"),
        (("eső",), "rain"),
        (("havaz", "hó"), "snow"),
        (("köd",), "fog"),
        (("fagy", "hideg"), "frost"),
        (("hőség", "meleg"), "heat"),
    ]

    def parse(self, page: Element) -> dict[str, Any]:
        """
        Parse the alerts of the hourly cards and the banner's warnings.

        The hourly cards' icons are alerts ("riasztás") for the coming hours;
        the banner at the top of the page is an alert or a warning
        ("figyelmeztetés"). Both are lists of {"type", "level"}: one item
        per type in force, at its highest level.
        """
        warnings = self._merge(self._parse_banners(page))
        return {
            "alerts": self._merge(self._parse_hourly_alerts(page)),
            "warnings": warnings,
            "warning_level": highest_alert_level(warnings),
        }

    def _parse_banners(self, page: Element) -> list[dict[str, str]]:
        """
        Return the banners at the top of the page.

        A banner holds only the level and the types, e.g. "Sárga
        figyelmeztetés zivatarra", "Sárga riasztás viharos szélre" or "Sárga
        figyelmeztetés zivatarra, viharos szélre" (<div class="...
        inlineWeatherAlert">).
        """
        return [
            item
            for box in page.find_all("div", "inlineWeatherAlert")
            for item in self._alerts_from_text(box.get_text(" ", strip=True))
        ]

    def _parse_hourly_alerts(self, page: Element) -> list[dict[str, str]]:
        """Return the alerts of the first hourly cards (from their icons' text)."""
        alerts = []
        for container in page.find_all("div", "genericHourlyAlert")[: self.HOURS]:
            link = container.find("a", "hover-over")
            if link is None:
                continue
            description = link.get("data-bs-content")
            if description is not None:
                alerts.extend(self._alerts_from_text(description))
        return alerts

    @classmethod
    def _alerts_from_text(cls, text: str) -> list[dict[str, str]]:
        """
        Return a {"type", "level"} for each type named in an alert text.

        The parts are separated by commas or "és", e.g. "Sárga figyelmeztetés
        zivatarra, viharos szélre"; a part without a level of its own has the
        level before it. Without any level the list is empty; with a level but
        no known type it is a single "other".
        """
        items = []
        level = None
        for part in re.split(r",|\bés\b", text):
            level = cls._level_from_text(part) or level
            alert_type = cls._known_type(part)
            if level is not None and alert_type is not None:
                items.append({"type": alert_type, "level": level})
        if not items and level is not None:
            items.append({"type": "other", "level": level})
        return items

    @staticmethod
    def _level_from_text(text: str) -> str | None:
        """Return the level named in an alert text (Sárga, Narancs, Piros)."""
        lowered = text.lower()
        if "sárga" in lowered:
            return "yellow"
        if "narancs" in lowered:
            return "orange"
        if "piros" in lowered or "vörös" in lowered:
            return "red"
        return None

    @classmethod
    def _known_type(cls, text: str) -> str | None:
        """Return the first alert type named in a text (None if none)."""
        lowered = text.lower()
        for words, alert_type in cls._TYPE_WORDS:
            if any(word in lowered for word in words):
                return alert_type
        return None

    @staticmethod
    def _merge(items: list[dict[str, str]]) -> list[dict[str, str]]:
        """
        Return one item per type, at its highest level.

        Highest level first; items of the same level in type order.
        """
        highest: dict[str, str] = {}
        for item in items:
            current = highest.get(item["type"], ALERT_NONE)
            if ALERT_LEVELS.index(item["level"]) > ALERT_LEVELS.index(current):
                highest[item["type"]] = item["level"]
        return [
            {"type": alert_type, "level": highest[alert_type]}
            for alert_type in sorted(
                highest,
                key=lambda t: (-ALERT_LEVELS.index(highest[t]), ALERT_TYPES.index(t)),
            )
        ]


def highest_alert_level(items: list[dict[str, str]] | None) -> str | None:
    """Return the highest level of the items ("none" if empty, None if unknown)."""
    if items is None:
        return None
    return max(
        (item["level"] for item in items),
        key=ALERT_LEVELS.index,
        default=ALERT_NONE,
    )



# The current hour's item values that also serve as "current" values:
# (hourly item key, data key)
CURRENT_HOUR_KEYS = (
    ("wind_bearing", "wind_bearing"),
    ("wind_strength", "wind_strength"),
    ("native_wind_speed", "wind_speed"),
    ("native_wind_gust_speed", "wind_gust_speed"),
    ("precipitation", "precipitation"),
    ("precipitation_probability", "precipitation_probability"),
)


def current_hour_values(item: dict[str, Any] | None) -> dict[str, Any]:
    """Return the "current" wind and precipitation from the current hour's item."""
    if item is None:
        return {}
    return {
        key: item[item_key]
        for item_key, key in CURRENT_HOUR_KEYS
        if item.get(item_key) is not None
    }


def hour_start(item: dict[str, Any]) -> dt.datetime | None:
    """Return an hourly item's start (None without a readable aware time)."""
    try:
        start = dt.datetime.fromisoformat(item["datetime"])
    except (KeyError, TypeError, ValueError):
        return None
    return start if start.tzinfo is not None else None


def interpolated_item(
    before: dict[str, Any], after: dict[str, Any], share: float
) -> dict[str, Any]:
    """
    Return the values a share (0-1) of the way between two hourly items.

    Numbers are interpolated, the precipitation to two decimals (0 without
    any) and the others to whole numbers, as the page's hours have them. The
    wind direction goes the shorter way round, the wind by its strength level,
    which gives its speed and gust. The condition, and a value that one of the
    two lacks, are the nearer one's (the later one's halfway). The datetime
    is left to the caller.
    """
    result = dict(before if share < 1 / 2 else after)

    def ends(key: str) -> tuple[Any, Any] | None:
        pair = before.get(key), after.get(key)
        return None if None in pair else pair

    def between(pair: tuple[Any, Any]) -> float:
        return pair[0] + (pair[1] - pair[0]) * share

    for key in ("temperature", "cloud_coverage", "precipitation_probability"):
        if pair := ends(key):
            result[key] = round_whole(between(pair))
    if pair := ends("precipitation"):
        result["precipitation"] = round_to(between(pair), 2) or 0
    if pair := ends("wind_bearing"):
        turn = (pair[1] - pair[0] + 180) % 360 - 180
        angle = (pair[0] + turn * share) % 360
        result["wind_bearing"] = round_whole(angle) % 360
    levels = WindStrengthMapper.levels()
    if (pair := ends("wind_strength")) and all(level in levels for level in pair):
        low, high = (levels.index(level) for level in pair)
        level = levels[round_whole(low + (high - low) * share)]
        result["wind_strength"] = level
        result["native_wind_speed"] = WindStrengthMapper.SPEEDS[level]
        result["native_wind_gust_speed"] = WindStrengthMapper.GUSTS[level]
    return result


def filled_hours(
    listed: list[dict[str, Any]], saved: list[dict[str, Any]], now: dt.datetime
) -> list[dict[str, Any]]:
    """
    Return a forecast's hourly items with none missing from the running hour on.

    The hours missing up to the last listed one are added. Each is the saved
    (earlier downloaded) item of its hour if there is one. Else it is
    interpolated between the listed or saved hours around it, hours already
    over included; with none before it, it is a copy of the first one after it.
    """
    one_hour = dt.timedelta(hours=1)
    # Every available item by its start, a listed one rather than a saved one
    known = {
        start: item
        for item in [*saved, *listed]
        if (start := hour_start(item)) is not None
    }
    available = sorted(known.items())
    # Counted in UTC, so that a change of the clocks doesn't matter
    hour = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    result: list[dict[str, Any]] = []
    for item in listed:
        start = dt.datetime.fromisoformat(item["datetime"])
        while hour < start:
            if hour in known:
                result.append(known[hour])
            else:
                local = hour.astimezone(TimeUtils.get_local_timezone())
                values = _missing_values(hour, available)
                result.append({**values, "datetime": local.isoformat()})
            hour += one_hour
        result.append(item)
        if start >= hour:
            hour = start + one_hour
    return result


def _missing_values(
    hour: dt.datetime, available: list[tuple[dt.datetime, dict[str, Any]]]
) -> dict[str, Any]:
    """
    Return the values of an hour that no available (start, item) is of.

    The items are sorted by their start and at least one is later than the hour.
    """
    index = bisect.bisect_left(available, hour, key=itemgetter(0))
    end, after = available[index]
    if not index:
        return after
    start, before = available[index - 1]
    return interpolated_item(before, after, (hour - start) / (end - start))


class HourlyForecastParser(WeatherParser):
    """
    Parser for hourly forecast data.

    idokep.hu lists only the coming hours (at 12:51 the list starts at
    13:00). The hours missing from the current one on are added (see
    filled_hours): from an earlier fetch, or from the hours around them, e.g.
    a copy of the next hour right after the entry was added.
    """

    def __init__(self, records: FailureRecords | None = None) -> None:
        """Initialize the parser."""
        super().__init__(records)
        # Hourly items of the last parse, to keep the hours missing from
        self._last_forecast: list[dict[str, Any]] = []

    def parse(self, page: Element) -> dict[str, Any]:
        """Parse hourly forecast data."""
        result = {}
        forecast = []
        local_tz = TimeUtils.get_local_timezone()
        now = dt.datetime.now(tz=local_tz)

        # Start from today as the base date
        # (hourly forecast begins with today's remaining hours)
        base_date = now.date()

        hourly_cards = page.find_all("div", "ik wide-hourly-forecast-card")

        last_hour = None
        current_date = base_date

        for card in hourly_cards:
            # Extract hour first to detect day transitions
            hour_div = card.find("div", "ik wide-hourly-forecast-hour")
            if hour_div is not None:
                hour_text = hour_div.text.strip()
                try:
                    hour_int = int(hour_text.split(":")[0])
                except ValueError:
                    # Skip cards with a non-numeric hour label (e.g. "Most")
                    LOGGER.debug("Skipping hourly card with hour %r", hour_text)
                    continue

                # If hour decreased, we moved to next day
                if last_hour is not None and hour_int < last_hour:
                    current_date += dt.timedelta(days=1)
                # The list starts with the next hour, so after 23:00 its
                # first card (0:00) is already tomorrow's
                if last_hour is None and self._is_tomorrow(hour_int, now):
                    current_date += dt.timedelta(days=1)

                last_hour = hour_int

            forecast_item = self._parse_hourly_card(
                card, hour_div, current_date, local_tz
            )
            if forecast_item:
                forecast.append(forecast_item)

        if forecast:
            forecast = filled_hours(forecast, self._last_forecast, now)
            self._last_forecast = forecast
            result["hourly_forecast"] = forecast
            result["storm_expected"] = self.storm_expected(forecast, now)
            result.update(current_hour_values(self._current_hour_item(forecast, now)))

        return result

    @staticmethod
    def _is_tomorrow(first_hour: int, now: dt.datetime) -> bool:
        """
        Return True if the list's first hour is tomorrow's.

        The first card is the next hour (or, from a page cached a little
        earlier, the current one), so it is at most a few hours ahead of now;
        an hour more than 12 hours before now must be tomorrow's.
        """
        return now.hour - first_hour > 12

    def restore_hourly_forecast(self, items: list[dict[str, Any]]) -> None:
        """Seed the hourly items restored from disk (for the missing hours)."""
        self._last_forecast = list(items)

    @staticmethod
    def storm_expected(forecast: list[dict[str, Any]], now: dt.datetime) -> bool:
        """Return True if the current or the next hour is stormy."""
        hour = now.replace(minute=0, second=0, microsecond=0)
        end = hour + dt.timedelta(hours=2)
        return any(
            item.get("storm") and hour <= dt.datetime.fromisoformat(item["datetime"]) < end
            for item in forecast
        )

    @staticmethod
    def _current_hour_item(
        forecast: list[dict[str, Any]], now: dt.datetime
    ) -> dict[str, Any] | None:
        """Return the item whose one-hour slot contains now (None if missing)."""
        for item in forecast:
            start = dt.datetime.fromisoformat(item["datetime"])
            if start <= now < start + dt.timedelta(hours=1):
                return item
        return None

    def _parse_hourly_card(
        self,
        card: Element,
        hour_div: Element | None,
        forecast_date: dt.date,
        local_tz: dt.tzinfo | None = None,
    ) -> dict[str, Any] | None:
        """Parse individual hourly forecast card."""
        temp_div = card.find("div", "ik tempValue")  # class unchanged

        if hour_div is None or temp_div is None:
            return None

        temp_a = temp_div.find("a")
        temp = None
        if temp_a is not None:
            with contextlib.suppress(ValueError):
                temp = int(temp_a.text.strip())

        condition = self.extract_condition(card)
        precipitation_probability = self.extract_precipitation_probability(card)

        try:
            # Extract time and combine with the provided date
            hour_text = hour_div.text.strip()
            hour_int = int(hour_text.split(":")[0])
            minute_int = int(hour_text.split(":")[1]) if ":" in hour_text else 0

            slot_start = dt.datetime.combine(
                forecast_date,
                dt.time(hour_int, minute_int),
                tzinfo=local_tz or TimeUtils.get_local_timezone(),
            )

            strength = self.extract_wind_strength(card)
            storm = {"storm": True} if self._is_stormy(card) else {}
            return {
                **storm,
                "datetime": slot_start.isoformat(),
                "temperature": temp,
                "condition": condition,
                "cloud_coverage": WeatherConditionMapper.cloud_coverage(
                    self._condition_text(card)
                ),
                "precipitation": self.extract_precipitation_amount(card),
                "precipitation_probability": precipitation_probability,
                "wind_bearing": self.extract_wind_bearing(card),
                "wind_strength": strength,
                # Estimates from the strength (km/h); the site gives no speed
                "native_wind_speed": WindStrengthMapper.SPEEDS.get(strength or ""),
                "native_wind_gust_speed": WindStrengthMapper.GUSTS.get(strength or ""),
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _wind_arrow(card: Element) -> Element | None:
        """Return the card's wind arrow."""
        # Its strength class varies (e.g. "ik wind mersekelt")
        for box in card.find_all("div", "hourly-wind"):
            if (arrow := box.find("div", "wind")) is not None:
                return arrow
        return None

    def extract_wind_bearing(self, card: Element) -> int | None:
        """
        Extract wind bearing in degrees from hourly card.

        The wind arrow is rotated via an inline CSS variable
        (e.g. style="--rotateAngle:6deg"); the angle is the direction the
        wind blows from, as Home Assistant expects.
        """
        wind_div = self._wind_arrow(card)
        if wind_div is None:
            return None

        style = wind_div.get("style", "")
        angle_match = re.search(r"--rotateAngle:\s*(-?\d+)deg", style)
        if not angle_match:
            return None
        return int(angle_match.group(1)) % 360

    def extract_wind_strength(self, card: Element) -> str | None:
        """
        Extract the wind strength level (e.g. "moderate_breeze") of a card.

        From the arrow's class (e.g. "ik wind mersekelt"), or else from the
        tooltip text (e.g. "Mérsékelt északi szél"). Unknown words give None.
        """
        wind_div = self._wind_arrow(card)
        if wind_div is None:
            return None
        classes = wind_div.classes
        for css_class in classes:
            if css_class not in ("ik", "wind"):
                level = WindStrengthMapper.from_class(css_class)
                if level is not None:
                    return level
        link = wind_div.find_parent("a")
        tooltip = None if link is None else link.get("data-bs-content")
        if tooltip is not None:
            level = WindStrengthMapper.from_text(tooltip)
            if level is not None:
                return level
        self._records.warn_once(
            LOGGER,
            "Unknown idokep.hu wind strength %r",
            " ".join(classes) if tooltip is None else tooltip,
        )
        return None

    @staticmethod
    def _condition_text(card: Element) -> str | None:
        """Return the condition's Hungarian text from the icon container tag."""
        icon_container = card.find("div", "forecast-icon-container")
        if icon_container is not None:
            icon_a = icon_container.find("a")
            if icon_a is not None:
                return icon_a.get("data-bs-content")
        return None

    def extract_condition(self, card: Element) -> str | None:
        """Extract weather condition from the icon container tag."""
        text = self._condition_text(card)
        if text is None:
            return None
        condition = WeatherConditionMapper.map_condition(text, self._records)
        if condition == "sunny" and self._has_night_icon(card):
            return "clear-night"
        return condition

    @staticmethod
    def _has_night_icon(card: Element) -> bool:
        """Return True if the card's icon is a night one (310.svg; by day 010.svg)."""
        icon_container = card.find("div", "forecast-icon-container")
        icon = None if icon_container is None else icon_container.find("img")
        source = "" if icon is None else icon.get("src", "")
        return bool(re.search(r"/3\d\d\.svg", source))

    def _is_stormy(self, card: Element) -> bool:
        """Return True if the card's condition is a storm."""
        return (self._condition_text(card) or "").lower() in STORM_TEXTS

    def extract_precipitation_probability(self, card: Element) -> int:
        """Extract precipitation probability."""
        rain_chance_div = card.find("div", "ik hourly-rain-chance")
        if rain_chance_div is None:
            return 0

        rain_a = rain_chance_div.find("a")
        if rain_a is None:
            return 0

        rain_text = rain_a.text.strip()
        if not rain_text.endswith("%"):
            return 0

        try:
            return int(rain_text[:-1])
        except ValueError:
            return 0

    def extract_precipitation_amount(self, card: Element) -> float:
        """
        Return the hour's precipitation in mm; 0 when the card shows none.

        The amount is in an HTML comment inside the rain bar (e.g.
        <div class="ik hourly-rainlevel"><!--16 mm--></div>), not shown on
        the page. Despite its "mm" it is in tenths of a millimetre: a day's
        hourly amounts add up to ten times the day's own amount on the same
        page (e.g. 161 against 16 mm), so 16 is 1.6 mm. The bar's height
        follows it but stops at 16 px (7 px: 0.1 mm, 12 px: 0.2 mm, 16 px:
        0.3 mm or more), so it is only used if the comment is missing.
        """
        level = card.find("div", "hourly-rainlevel")
        if level is not None:
            for comment in level.comments():
                match = re.search(r"(\d+(?:[.,]\d+)?)", comment)
                if match:
                    return round_to(float(match.group(1).replace(",", ".")) / 10, 2)
        meter = card.find("div", "hourly-rainlevel-meter")
        style = "" if meter is None else meter.get("style", "")
        match = re.search(r"height:\s*(\d+)px", style)
        if not match:
            return 0
        height = int(match.group(1))
        if height <= 0:
            return 0
        if height <= 9:  # 7 px bar
            return 0.1
        return 0.2 if height <= 14 else 0.3  # 12 px / 16 px bars


def daily_item_from_hours(
    hours: list[dict[str, Any]], day: dt.date
) -> dict[str, Any] | None:
    """
    Return a day's daily entry made from its hourly items.

    The highest and lowest temperature, the precipitation's sum, the most
    frequent condition and the mean cloud coverage of the hours given: late in
    the day only the rest of it. None if no hour of the day has a temperature.
    """
    own = [
        item
        for item in hours
        if str(item.get("datetime", ""))[:10] == day.isoformat()
    ]

    def values(key: str) -> list[Any]:
        return [item[key] for item in own if item.get(key) is not None]

    temperatures = values("temperature")
    if not temperatures:
        return None
    # A day isn't called a clear night; of equally frequent ones the first
    conditions = Counter(
        "sunny" if condition == "clear-night" else condition
        for condition in values("condition")
    )
    cloud_coverage = values("cloud_coverage")
    precipitation = values("precipitation")
    return {
        "datetime": TimeUtils.day_start(day),
        "temperature": max(temperatures),
        "templow": min(temperatures),
        "condition": conditions.most_common(1)[0][0] if conditions else None,
        "cloud_coverage": (
            round_whole(sum(cloud_coverage) / len(cloud_coverage))
            if cloud_coverage
            else None
        ),
        "precipitation": (
            round_to(sum(precipitation), 2) if precipitation else None
        ),
    }


class DailyForecastParser(WeatherParser):
    """
    Parser for the daily forecast columns.

    Used on the forecast page (/elorejelzes/), which also holds a strip of
    daily columns (about 23 days). It also gives today's max/min.

    In the evening idokep.hu drops today's column (the strip then starts with
    tomorrow). Today's entry seen earlier is kept and still reported, both in
    the daily forecast and as today's max/min, until midnight. Without one, it
    is made from today's hourly items (see add_today_from_hours).
    """

    def __init__(self, records: FailureRecords | None = None) -> None:
        """Initialize the parser."""
        super().__init__(records)
        # Today's daily forecast entry, kept for when the page drops it
        self._today_item: dict[str, Any] | None = None

    _MONTHS: ClassVar[dict[str, int]] = {
        "január": 1,
        "február": 2,
        "március": 3,
        "április": 4,
        "május": 5,
        "június": 6,
        "július": 7,
        "augusztus": 8,
        "szeptember": 9,
        "október": 10,
        "november": 11,
        "december": 12,
    }

    def extract_date(self, col: Element) -> dt.date | None:
        """
        Return the date a daily column is for.

        The icon link's title holds it, e.g. "Péntek<br>2026. október 30.".
        """
        title = next(
            (
                link.attrs["title"]
                for box in col.find_all(class_="dfIconAlert")
                for link in box.find_all("a")
                if "title" in link.attrs
            ),
            None,
        )
        if title is None:
            return None
        match = re.search(r"(\d{4})\.\s*(\w+)\s+(\d{1,2})\.", title)
        if not match or match.group(2).lower() not in self._MONTHS:
            return None
        try:
            return dt.date(
                int(match.group(1)),
                self._MONTHS[match.group(2).lower()],
                int(match.group(3)),
            )
        except ValueError:
            return None

    def parse(self, page: Element) -> dict[str, Any]:
        """Parse daily forecast data."""
        result = {}
        daily_forecast = []
        daily_cols = page.find_all("div", "ik dailyForecastCol")
        today = dt.datetime.now(tz=TimeUtils.get_local_timezone()).date()

        # Each column carries its own date: in the evening idokep.hu drops
        # today's column, so the strip does not always start with today
        previous_date: dt.date | None = None
        for col in daily_cols:
            forecast_date = self.extract_date(col)
            if forecast_date is None:
                # Unreadable date: assume the day after the previous column
                forecast_date = (
                    today
                    if previous_date is None
                    else previous_date + dt.timedelta(days=1)
                )
            previous_date = forecast_date
            if forecast_date < today:
                # e.g. a page cached just before midnight
                continue

            forecast_item = self._parse_daily_column(col, forecast_date)
            if forecast_date == today:
                self._today_item = forecast_item
            daily_forecast.append(forecast_item)

        # Today's column dropped (evening): keep showing today until midnight
        kept = self._kept_today_item(today)
        if kept is not None and not any(
            item["datetime"] == kept["datetime"] for item in daily_forecast
        ):
            daily_forecast.insert(0, kept)

        if daily_forecast:
            result["daily_forecast"] = daily_forecast

        return result

    def add_today_from_hours(self, hours: list[dict[str, Any]]) -> None:
        """
        Make today's entry from its hours while none is remembered.

        Call it before parsing the page: a column for today replaces it.
        """
        today = dt.datetime.now(tz=TimeUtils.get_local_timezone()).date()
        if self._kept_today_item(today) is None:
            self._today_item = daily_item_from_hours(hours, today)

    def restore_today_forecast(self, item: dict[str, Any]) -> None:
        """Remember today's daily entry from before a restart."""
        self._today_item = item

    def parse_today_min_max(self, page: Element) -> dict[str, int]:
        """
        Extract today's forecast max and min temperature.

        Taken from the daily column dated today, or from today's entry kept
        from earlier today once the page has dropped that column.
        """
        today = dt.datetime.now(tz=TimeUtils.get_local_timezone()).date()
        for col in page.find_all("div", "ik dailyForecastCol"):
            if self.extract_date(col) == today:
                self._today_item = self._parse_daily_column(col, today)
                break

        kept = self._kept_today_item(today)
        if kept is None:
            return {}
        result = {}
        if kept.get("temperature") is not None:
            result["temperature_max_today"] = kept["temperature"]
        if kept.get("templow") is not None:
            result["temperature_min_today"] = kept["templow"]
        return result

    def _kept_today_item(self, today: dt.date) -> dict[str, Any] | None:
        """Return the remembered entry if it is for today (else forget it)."""
        if self._today_item is None:
            return None
        # The date part, so a date-only entry saved by an earlier version counts
        if str(self._today_item.get("datetime", ""))[:10] != today.isoformat():
            self._today_item = None
            return None
        return self._today_item

    def _parse_daily_column(self, col: Element, forecast_date: dt.date) -> dict[str, Any]:
        """Parse individual daily forecast column."""
        min_temp, max_temp = self.extract_temperatures(col)
        text = self._condition_text(col)
        condition = (
            WeatherConditionMapper.map_condition(text, self._records)
            if text
            else None
        )
        precipitation = self.extract_precipitation(col)

        return {
            "datetime": TimeUtils.day_start(forecast_date),
            "temperature": max_temp,
            "templow": min_temp,
            "condition": condition,
            "cloud_coverage": WeatherConditionMapper.cloud_coverage(text),
            "precipitation": precipitation,
        }

    def extract_temperatures(self, col: Element) -> tuple[int | None, int | None]:
        """
        Extract min and max temperatures from column.

        Returns:
            tuple: (min_temp, max_temp)

        """
        # First check for min-max-close or min-max-closer div (when temps are close)
        close_div = col.find("div", ("ik min-max-close", "ik min-max-closer"))
        if close_div is not None:
            a_tags = close_div.find_all("a")
            min_required_tags = 2
            if len(a_tags) >= min_required_tags:
                # First <a> is max, second is min
                max_match = re.search(r"(-?\d+)", a_tags[0].get_text(strip=True))
                min_match = re.search(r"(-?\d+)", a_tags[1].get_text(strip=True))
                max_temp = int(max_match.group(1)) if max_match else None
                min_temp = int(min_match.group(1)) if min_match else None
                return (min_temp, max_temp)

        # Otherwise, look for separate max and min divs
        min_temp = self.extract_temperature(col, "ik min")
        max_temp = self.extract_temperature(col, "ik max")
        return (min_temp, max_temp)

    def extract_temperature(self, col: Element, class_name: str) -> int | None:
        """Extract temperature from column."""
        temp_div = col.find("div", class_name)
        if temp_div is not None:
            temp_a = temp_div.find("a")
            if temp_a is not None:
                match = re.search(r"(-?\d+)", temp_a.get_text(strip=True))
                if match:
                    return int(match.group(1))
        return None

    def extract_condition(self, col: Element) -> str | None:
        """Extract weather condition from column."""
        text = self._condition_text(col)
        if not text:
            return None
        return WeatherConditionMapper.map_condition(text, self._records)

    @staticmethod
    def _condition_text(col: Element) -> str | None:
        """Return the day's Hungarian description from the column's popover."""
        icon_alert = col.find("div", "ik dfIconAlert")
        if icon_alert is None:
            return None

        a_tag = icon_alert.find("a")
        if a_tag is None:
            return None

        popover = a_tag.get("data-bs-content")
        if popover is None:
            return None

        # The popover HTML contains the forecast icon img; extract condition from
        # its alt attribute (works even when additional attributes like src exist):
        # e.g. <img class='ik popover-icon' src='...forecastIcons/...' alt='zápor'>
        alt_match = re.search(
            r"forecastIcons/[^'\"]+['\"][^>]*alt=['\"]([^'\"]+)['\"]"
            r"|alt=['\"]([^'\"]+)['\"][^>]*forecastIcons/",
            popover,
        )
        if alt_match:
            condition_text = (alt_match.group(1) or alt_match.group(2) or "").strip()
            if condition_text:
                return condition_text

        # Fallback: grab text immediately after any forecast icon img closing >
        text_match = re.search(r"forecastIcons/[^>]+>([^<]+)", popover)
        if text_match:
            return text_match.group(1).strip()

        return None

    def extract_precipitation(self, col: Element) -> float:
        """
        Extract the day's precipitation amount in mm (e.g. "0,5 mm" -> 0.5).

        Below 2 mm the page prints no number, only "." ("Elszórtan",
        scattered); the amount then comes from the day's rain bar, which is
        always 2 px per mm + 1 px (2 mm: 5 px, 5 mm: 11 px; ".": 3 px = 1 mm).
        """
        precip_span = col.find("span", "ik mm")
        if precip_span is not None:
            match = re.search(r"(\d+(?:[.,]\d+)?)", precip_span.text)
            if match:
                return float(match.group(1).replace(",", "."))
        rain_bar = col.find("div", "rainlevel")
        if rain_bar is not None:
            style = rain_bar.get("style", "")
            match = re.search(r"height:\s*(\d+)px", style)
            if match and int(match.group(1)) > 1:
                return round_to((int(match.group(1)) - 1) / 2, 2)
        return 0


# One station on a map: the link holds its code ("#" if it has no page), the
# tooltip starts with the value and the settlement's name, as either
# Tip("<b>59%</b><br><b>Name</b>... or Tip("1029.2 hPa <b>Name</b>...
_MAP_STATION = re.compile(
    r"href='([^']*)'[^>]*?Tip\(\"\s*(?:<b>)?(-?\d+(?:\.\d+)?)[^<]*(?:</b><br>)?\s*<b>([^<]*)</b>"
)


def _clean_name(name: str) -> str:
    """Return a name in a comparable form (the pages use non-breaking spaces)."""
    return " ".join(html.unescape(name).split()).casefold()


def _is_part_of(name: str, location: str) -> bool:
    """Return True if a station's name is the settlement's, then a separator."""
    return (
        len(name) > len(location)
        and name.startswith(location)
        and not name[len(location)].isalnum()
    )


def _settlement_average(
    stations: list[tuple[str, float]], location: str
) -> float | None:
    """
    Return the average of a settlement's (name, value) stations.

    Stations named exactly as the settlement count; if there are none, those
    named after it (Budapest: "Budapest XI. ker"). None if it has no station.
    """
    wanted = _clean_name(location)
    values = [value for name, value in stations if name == wanted] or [
        value for name, value in stations if _is_part_of(name, wanted)
    ]
    return round_to(sum(values) / len(values), 2) if values else None


def station_map_average(page: str, map_name: str, location: str) -> float | None:
    """
    Return the settlement's average on a map page (see _settlement_average).

    The pages are large and only these tooltips are needed, so they are read
    with a regular expression instead of being parsed as a whole.
    """
    start = page.find(f'<map name="{map_name}">')
    if start < 0:
        msg = f"Map {map_name} not found on the page"
        raise IdokepApiClientCommunicationError(msg)
    end = page.find("</map>", start)
    section = page[start : end if end >= 0 else len(page)]

    # A station can be listed twice on a map; one without a code has no page
    stations: dict[tuple[str, str], tuple[str, float]] = {}
    for code, value, name in _MAP_STATION.findall(section):
        key = (code, "") if code != "#" else (code, name + value)
        stations.setdefault(key, (_clean_name(name), float(value)))
    return _settlement_average(list(stations.values()), location)


class IdokepApiClient:
    """Időkép client: one fetch method per idokep.hu page."""

    def __init__(
        self, session: aiohttp.ClientSession, records: FailureRecords | None = None
    ) -> None:
        """
        Initialize the API client.

        records: what is logged already, shared by the parsers.
        """
        records = records or FailureRecords()
        self._http_client = HttpClient(session)
        self._current_parser = CurrentWeatherParser(records)
        self._hourly_parser = HourlyForecastParser(records)
        self._daily_parser = DailyForecastParser(records)
        self._alert_parser = AlertParser(records)

    # Each method raises IdokepApiClientCommunicationError on network/HTTP
    # failure, so the caller can tell a failed fetch from a page without data.
    async def async_check_connection(self, location: str) -> None:
        """
        Check that at least one of the settlement's two pages is served.

        The current conditions page is tried first, then the forecast page.
        """
        try:
            await self._http_client.check(
                IdokepConfig.get_current_weather_url(location)
            )
        except IdokepApiClientCommunicationError:
            await self._http_client.check(
                IdokepConfig.get_hourly_forecast_url(location)
            )

    async def async_validate_location(self, location: str) -> str:
        """
        Check that idokep.hu knows the settlement and return its spelling.

        The current conditions page has to be titled with the name asked for
        (letter case and accents aside). The title's spelling is returned, as
        the maps' station names only match with their accents. Raises
        IdokepApiClientUnknownLocationError for another settlement's page, and
        IdokepApiClientCommunicationError for a page without weather data.

        While that page can't be read, the forecast page can still show that
        the name is unknown, but not that it is right: the site has answered
        some unknown names with a server error on the current conditions page.
        """
        try:
            return await self._current_page_spelling(location)
        except IdokepApiClientCommunicationError:
            await self._refuse_another_forecast(location)
            raise

    async def _current_page_spelling(self, location: str) -> str:
        """Return the name as the current conditions page spells it."""
        url = IdokepConfig.get_current_weather_url(location)
        page = await self._get_page(url)
        served = _titled_settlement(page, CURRENT_TITLE_END)
        if served is None or "temperature" not in self._current_parser.parse(page):
            msg = f"No weather data on {url}"
            raise IdokepApiClientCommunicationError(msg)
        _require_settlement(served, location)
        return " ".join(served.split())

    async def _refuse_another_forecast(self, location: str) -> None:
        """
        Raise if the forecast page served is another settlement's.

        Nothing is raised if it can't be downloaded or names no settlement.
        """
        try:
            page = await self._get_page(
                IdokepConfig.get_hourly_forecast_url(location)
            )
        except IdokepApiClientCommunicationError:
            return
        served = _titled_settlement(page, FORECAST_TITLE_END)
        if served is not None:
            _require_settlement(served, location)

    async def async_get_current_weather(self, location: str) -> dict[str, Any]:
        """Fetch and parse the current conditions page."""
        page = await self._get_page(IdokepConfig.get_current_weather_url(location))
        return self._current_parser.parse(page)

    async def async_get_humidity_map(self, location: str) -> dict[str, float | None]:
        """
        Fetch the settlement's humidity and dew point from /para.

        Both are always in the result; None if the settlement has no station
        on that map.
        """
        page = await self._http_client.get_html(IdokepConfig.get_humidity_url())
        return {
            "humidity": station_map_average(page, "rh", location),
            "dew_point": station_map_average(page, "td", location),
        }

    async def async_get_pressure_map(self, location: str) -> dict[str, float | None]:
        """
        Fetch the settlement's pressure from /legnyomas.

        Always in the result; None if the settlement has no station on the map.
        """
        page = await self._http_client.get_html(IdokepConfig.get_pressure_url())
        return {"pressure": station_map_average(page, "legnyomasterkep", location)}

    async def async_get_uv_map(self, location: str) -> dict[str, float | None]:
        """
        Fetch the settlement's UV index from /uv.

        Always in the result; None if the settlement has no station on the map.
        """
        page = await self._http_client.get_html(IdokepConfig.get_uv_url())
        return {"uv_index": station_map_average(page, "uv", location)}

    async def async_get_hourly_forecast(self, location: str) -> dict[str, Any]:
        """
        Fetch the forecast page and parse everything on it.

        Hourly forecast, alerts, daily forecast and today's max/min all come
        from this one download.
        """
        page = await self._get_page(IdokepConfig.get_hourly_forecast_url(location))
        hourly = self._hourly_parser.parse(page)
        self._daily_parser.add_today_from_hours(hourly.get("hourly_forecast", []))
        result = {
            **hourly,
            **self._alert_parser.parse(page),
            **self._daily_parser.parse(page),
            **self._daily_parser.parse_today_min_max(page),
        }
        return result

    def restore_today_forecast(self, item: dict[str, Any]) -> None:
        """Seed today's daily entry restored from disk (kept until midnight)."""
        self._daily_parser.restore_today_forecast(item)

    def restore_hourly_forecast(self, items: list[dict[str, Any]]) -> None:
        """Seed the hourly items restored from disk (for the missing hours)."""
        self._hourly_parser.restore_hourly_forecast(items)

    async def _get_page(self, url: str) -> Element:
        """Download a page and return it parsed."""
        return parse_html(await self._http_client.get_html(url))
