"""Refactored Időkép API Client with improved separation of concerns."""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import socket
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp
import async_timeout
from bs4 import BeautifulSoup, Tag

try:
    import zoneinfo
except ImportError:
    zoneinfo = None

from .const import LOGGER


@dataclass
class AlertData:
    """Single weather alert."""

    level: str  # "yellow", "orange", "red"
    type: str  # Alert type (e.g., "ónos eső", "vihar", "szél")
    description: str  # Full description
    icon_url: str | None = None  # URL to alert icon


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


# Exception classes remain the same
class IdokepApiClientError(Exception):
    """Exception to indicate a general API error."""


class IdokepApiClientCommunicationError(IdokepApiClientError):
    """Exception to indicate a communication error."""


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
        "jégeső": "hail",
        "havazás": "snowy",
        "intenzív havazás": "snowy",
        "hószállingózás": "snowy",
        "hófúvás": "snowy",
        "hófúvás havazással": "snowy",
        "hózápor": "snowy",
        "havas eső": "snowy-rainy",
        "fagyott eső": "snowy-rainy",
        "ónos eső": "snowy-rainy",
        "szeles": "windy",
    }

    @classmethod
    def map_condition(cls, condition: str) -> str:
        """Map Hungarian condition to Home Assistant standard condition."""
        return cls._CONDITION_MAPPING.get(condition.lower(), "unknown")

    @classmethod
    def conditions(cls) -> list[str]:
        """Return every condition map_condition can produce (except unknown)."""
        return sorted(set(cls._CONDITION_MAPPING.values()))


class WindStrengthMapper:
    """
    Maps idokep.hu wind strength words to Beaufort-scale levels.

    idokep.hu gives no wind speed, only a strength word ("Mérsékelt északi
    szél", class "mersekelt"). The estimated speed of a level is the middle of
    its Beaufort range in km/h (hurricane: its lower limit). Only "gyenge",
    "mersekelt" and "elenk" have been seen on the site so far; the other words
    follow the Hungarian Beaufort names.
    """

    # Level code: estimated speed in km/h, calm to hurricane (Beaufort 0-12)
    SPEEDS: ClassVar[dict[str, int]] = {
        "calm": 0,
        "light_air": 3,
        "light_breeze": 9,
        "gentle_breeze": 16,
        "moderate_breeze": 24,
        "fresh_breeze": 34,
        "strong_breeze": 44,
        "near_gale": 56,
        "gale": 68,
        "strong_gale": 82,
        "storm": 96,
        "violent_storm": 110,
        "hurricane": 118,
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
        return (
            zoneinfo.ZoneInfo("Europe/Budapest")
            if zoneinfo is not None
            else dt.timezone(dt.timedelta(hours=2))
        )


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
        try:
            async with (
                async_timeout.timeout(IdokepConfig.TIMEOUT),
                self._session.get(url, headers=_BROWSER_HEADERS) as response,
            ):
                response.raise_for_status()
                return await response.text()
        except TimeoutError as exception:
            msg = f"Timeout error fetching {url} - {exception}"
            raise IdokepApiClientCommunicationError(msg) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            msg = f"Error fetching {url} - {exception}"
            raise IdokepApiClientCommunicationError(msg) from exception


# Abstract base for parsers
class WeatherParser(ABC):
    """Abstract base class for weather data parsers."""

    @abstractmethod
    def parse(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Parse weather data from BeautifulSoup object."""


# Specific parser implementations
class CurrentWeatherParser(WeatherParser):
    """Parser for current weather data."""

    def parse(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Parse current weather data."""
        result = {}

        # Temperature
        temp_div = soup.find("div", class_="current-temperature")
        if isinstance(temp_div, Tag):
            # Match both ASCII 'C' and the Unicode DEGREE CELSIUS sign '\u2103'
            match = re.search(r"(-?\d+)[^\d]*(?:C|\u2103)", temp_div.text)
            if match:
                result["temperature"] = int(match.group(1))

        # Weather condition (class has no 'ik' prefix since 2026 redesign)
        cond_div = soup.find("div", class_="current-weather")
        if isinstance(cond_div, Tag):
            condition = cond_div.text.strip()
            result["condition"] = WeatherConditionMapper.map_condition(condition)

        return result


class AlertParser(WeatherParser):
    """Parser for weather alerts."""

    # Mapping of Hungarian alert types to English names
    ALERT_TYPE_MAP: ClassVar[dict[str, str]] = {
        "ónos eső": "freezing_rain",
        "ónosesőre": "freezing_rain",
        "vihar": "storm",
        "zivatar": "thunderstorm",
        "szél": "wind",
        "hó": "snow",
        "eső": "rain",
        "köd": "fog",
        "hőség": "heat",
        "hideg": "cold",
        "fagy": "frost",
    }

    def parse(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Parse weather alerts from page."""
        result: dict[str, Any] = {"alerts": []}

        # Parse general alert bar
        general_alerts = self._parse_general_alert(soup)
        result["alerts"].extend(general_alerts)

        # Parse hourly forecast alerts
        hourly_alerts = self._parse_hourly_alerts(soup)
        result["alerts"].extend(hourly_alerts)

        # Organize alerts by level
        result["alerts_by_level"] = self._organize_by_level(result["alerts"])

        return result

    def _parse_general_alert(self, soup: BeautifulSoup) -> list[AlertData]:
        """Parse the general alert bar at top of page."""
        alerts = []
        alert_bar = soup.find("div", id="topalertbar")

        if not alert_bar or not isinstance(alert_bar, Tag):
            return alerts

        # Determine alert level from class
        level = None
        class_attr = alert_bar.get("class", [])
        if isinstance(class_attr, list):
            if "yellow" in class_attr:
                level = "yellow"
            elif "orange" in class_attr:
                level = "orange"
            elif "red" in class_attr:
                level = "red"

        if not level:
            return alerts

        # Extract alert text
        link = alert_bar.find("a")
        if link and isinstance(link, Tag):
            description = link.get_text(strip=True)
            # Remove the icon text
            description = re.sub(r"^[\s\S]*?riasztás", "riasztás", description)
            description = description.strip()

            # Extract alert type
            alert_type = self._extract_alert_type(description)

            alerts.append(
                AlertData(
                    level=level,
                    type=alert_type,
                    description=description,
                    icon_url=None,
                )
            )

        return alerts

    def _parse_hourly_alerts(self, soup: BeautifulSoup) -> list[AlertData]:
        """Parse hourly forecast alert icons."""
        alerts = []
        seen_alerts = set()  # To avoid duplicates

        # Find all hourly alert containers
        alert_containers = soup.find_all("div", class_="genericHourlyAlert")

        for container in alert_containers:
            if not isinstance(container, Tag):
                continue

            # Find the alert link/image
            alert_link = container.find("a", class_="hover-over")
            if not alert_link or not isinstance(alert_link, Tag):
                continue

            # Extract alert description from data-bs-content
            description = alert_link.get("data-bs-content", "")
            if not description or not isinstance(description, str):
                continue

            # Determine level from description
            level = None
            if "Sárga" in description or "sárga" in description:
                level = "yellow"
            elif "Narancs" in description or "narancs" in description:
                level = "orange"
            elif (
                "Piros" in description
                or "piros" in description
                or "Vörös" in description
            ):
                level = "red"

            if not level:
                continue

            # Extract icon URL
            img = alert_link.find("img", class_="forecast-alert-icon")
            icon_url = None
            if img and isinstance(img, Tag):
                src = img.get("src")
                if src and isinstance(src, str):
                    icon_url = (
                        f"https://www.idokep.hu{src}" if src.startswith("/") else src
                    )

            # Extract alert type
            alert_type = self._extract_alert_type(description)

            # Avoid duplicate alerts
            alert_key = (level, alert_type, description)
            if alert_key not in seen_alerts:
                seen_alerts.add(alert_key)
                alerts.append(
                    AlertData(
                        level=level,
                        type=alert_type,
                        description=description,
                        icon_url=icon_url,
                    )
                )

        return alerts

    def _extract_alert_type(self, description: str) -> str:
        """Extract standardized alert type from description."""
        description_lower = description.lower()

        for hungarian, english in self.ALERT_TYPE_MAP.items():
            if hungarian in description_lower:
                return english

        # Default to generic alert
        return "general"

    def _organize_by_level(self, alerts: list[AlertData]) -> dict[str, list[dict]]:
        """Organize alerts by severity level."""
        by_level = {"yellow": [], "orange": [], "red": []}

        for alert in alerts:
            alert_dict = {
                "type": alert.type,
                "description": alert.description,
                "icon_url": alert.icon_url,
            }
            by_level[alert.level].append(alert_dict)

        return by_level


class HourlyForecastParser(WeatherParser):
    """
    Parser for hourly forecast data.

    idokep.hu lists only the coming hours (at 12:51 the list starts at
    13:00). The current hour's entry seen on an earlier fetch is kept and
    still reported first until the hour ends.
    """

    def __init__(self) -> None:
        """Initialize the parser."""
        # Hourly items of the last parse, to keep the current hour from
        self._last_forecast: list[dict[str, Any]] = []

    def parse(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Parse hourly forecast data."""
        result = {}
        forecast = []
        local_tz = TimeUtils.get_local_timezone()
        now = dt.datetime.now(tz=local_tz)

        # Start from today as the base date
        # (hourly forecast begins with today's remaining hours)
        base_date = now.date()

        hourly_cards = soup.find_all("div", class_="ik wide-hourly-forecast-card")

        last_hour = None
        current_date = base_date

        for card in hourly_cards:
            if not isinstance(card, Tag):
                continue

            # Extract hour first to detect day transitions
            hour_div = card.find("div", class_="ik wide-hourly-forecast-hour")
            if hour_div and isinstance(hour_div, Tag):
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

                last_hour = hour_int

            forecast_item = self._parse_hourly_card(
                card, hour_div, current_date, local_tz
            )
            if forecast_item:
                forecast.append(forecast_item)

        if forecast:
            forecast = self._with_current_hour(forecast, now)
            self._last_forecast = forecast
            result["hourly_forecast"] = forecast
            current = self._current_hour_item(forecast, now)
            # The current hour's wind also serves as "current" wind
            for item_key, key in (
                ("wind_bearing", "wind_bearing"),
                ("wind_strength", "wind_strength"),
                ("native_wind_speed", "wind_speed"),
            ):
                if current and current.get(item_key) is not None:
                    result[key] = current[item_key]

        return result

    def restore_hourly_forecast(self, items: list[dict[str, Any]]) -> None:
        """Seed the hourly items restored from disk (to keep the current hour)."""
        self._last_forecast = list(items)

    def _with_current_hour(
        self, forecast: list[dict[str, Any]], now: dt.datetime
    ) -> list[dict[str, Any]]:
        """Return forecast with the current hour's kept item put first if missing."""
        first_start = dt.datetime.fromisoformat(forecast[0]["datetime"])
        if first_start <= now:
            return forecast
        for item in self._last_forecast:
            try:
                start = dt.datetime.fromisoformat(item["datetime"])
            except (KeyError, TypeError, ValueError):
                continue
            if start.tzinfo is None:
                continue
            # The page starts after now, so this slot is before its first one
            if start <= now < start + dt.timedelta(hours=1):
                return [item, *forecast]
        return forecast

    @staticmethod
    def _current_hour_item(
        forecast: list[dict[str, Any]], now: dt.datetime
    ) -> dict[str, Any] | None:
        """Return the forecast item whose one-hour slot contains now."""
        for item in forecast:
            start = dt.datetime.fromisoformat(item["datetime"])
            if start <= now < start + dt.timedelta(hours=1):
                return item
        # Page may already start at the next hour; fall back to the first slot
        return forecast[0]

    def _parse_hourly_card(
        self,
        card: Tag,
        hour_div: Tag | None,
        forecast_date: dt.date,
        local_tz: dt.tzinfo | None = None,
    ) -> dict[str, Any] | None:
        """Parse individual hourly forecast card."""
        temp_div = card.find("div", class_="ik tempValue")  # class unchanged

        if not (
            hour_div
            and temp_div
            and isinstance(hour_div, Tag)
            and isinstance(temp_div, Tag)
        ):
            return None

        temp_a = temp_div.find("a")
        temp = None
        if temp_a and isinstance(temp_a, Tag):
            with contextlib.suppress(ValueError):
                temp = int(temp_a.text.strip())

        condition = self.extract_condition(card)
        precipitation, precipitation_probability = self.extract_precipitation_data(card)

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
            return {
                "datetime": slot_start.isoformat(),
                "temperature": temp,
                "condition": condition,
                "precipitation": precipitation,
                "precipitation_probability": precipitation_probability,
                "wind_bearing": self.extract_wind_bearing(card),
                "wind_strength": strength,
                # Estimate from the strength (km/h); the site gives no speed
                "native_wind_speed": WindStrengthMapper.SPEEDS.get(strength or ""),
            }
        except (ValueError, IndexError):
            return None

    def extract_wind_bearing(self, card: Tag) -> int | None:
        """
        Extract wind bearing in degrees from hourly card.

        The wind arrow is rotated via an inline CSS variable
        (e.g. style="--rotateAngle:6deg"); the angle is the direction the
        wind blows from, as Home Assistant expects.
        """
        # The strength class varies (e.g. "ik wind mersekelt"), so match by CSS
        wind_div = card.select_one("div.hourly-wind div.wind")
        if not wind_div:
            return None

        style = wind_div.get("style", "")
        if not isinstance(style, str):
            return None

        angle_match = re.search(r"--rotateAngle:\s*(-?\d+)deg", style)
        if not angle_match:
            return None
        return int(angle_match.group(1)) % 360

    def extract_wind_strength(self, card: Tag) -> str | None:
        """
        Extract the wind strength level (e.g. "moderate_breeze") of a card.

        From the arrow's class (e.g. "ik wind mersekelt"), or else from the
        tooltip text (e.g. "Mérsékelt északi szél"). Unknown words give None.
        """
        wind_div = card.select_one("div.hourly-wind div.wind")
        if not wind_div:
            return None
        classes = wind_div.get("class") or []
        for css_class in classes:
            if css_class not in ("ik", "wind"):
                level = WindStrengthMapper.from_class(css_class)
                if level is not None:
                    return level
        link = wind_div.find_parent("a")
        tooltip = link.get("data-bs-content") if link else None
        if isinstance(tooltip, str):
            level = WindStrengthMapper.from_text(tooltip)
            if level is not None:
                return level
        LOGGER.debug("Unknown wind strength: classes %s, tooltip %r", classes, tooltip)
        return None

    def extract_condition(self, card: Tag) -> str | None:
        """Extract weather condition from the icon container tag."""
        icon_container_elem = card.find("div", class_="forecast-icon-container")
        icon_container = (
            icon_container_elem if isinstance(icon_container_elem, Tag) else None
        )
        condition = None
        if icon_container and isinstance(icon_container, Tag):
            icon_a = icon_container.find("a")
            if icon_a and isinstance(icon_a, Tag):
                condition_val = icon_a.get("data-bs-content")
                if isinstance(condition_val, str):
                    condition = WeatherConditionMapper.map_condition(condition_val)
        return condition

    def extract_precipitation_data(self, card: Tag) -> tuple[int, int]:
        """Extract precipitation data from hourly card."""
        precipitation_probability = self.extract_precipitation_probability(card)
        precipitation = self.extract_precipitation_amount(card)
        return precipitation, precipitation_probability

    def extract_precipitation_probability(self, card: Tag) -> int:
        """Extract precipitation probability."""
        rain_chance_div = card.find("div", class_="ik hourly-rain-chance")
        if not (rain_chance_div and isinstance(rain_chance_div, Tag)):
            return 0

        rain_a = rain_chance_div.find("a")
        if not (rain_a and isinstance(rain_a, Tag)):
            return 0

        rain_text = rain_a.text.strip()
        if not rain_text.endswith("%"):
            return 0

        try:
            return int(rain_text[:-1])
        except ValueError:
            return 0

    def extract_precipitation_amount(self, card: Tag) -> int:
        """
        Extract precipitation amount from hourly card.

        The new frontend no longer encodes mm values as class names.
        We detect the presence of a non-N/A rainlevel div and estimate from
        the inline height style (each pixel roughly represents ~1 mm).
        Returns 0 when no rain is indicated.
        """
        # rainlevel-na means no precipitation
        if card.find("div", class_="ik rainlevel-na"):
            return 0

        # Look for a generic rainlevel div (new format uses style height)
        rainlevel_div = card.find("div", class_="ik rainlevel")
        if not (rainlevel_div and isinstance(rainlevel_div, Tag)):
            return 0

        # Try to parse height from inline style (e.g. style="height: 5px;")
        style = rainlevel_div.get("style", "")
        if isinstance(style, str):
            height_match = re.search(r"height:\s*(\d+)px", style)
            if height_match:
                # Each pixel is roughly proportional to mm; treat as integer mm
                return int(height_match.group(1))

        # Rainlevel div exists but height unknown - indicate some precipitation
        return 1


class DailyForecastParser(WeatherParser):
    """
    Parser for the daily forecast columns.

    Used on the forecast page (/elorejelzes/), which also holds a strip of
    daily columns (about 23 days). It also gives today's max/min.

    In the evening idokep.hu drops today's column (the strip then starts with
    tomorrow). Today's entry seen earlier is kept and still reported, both in
    the daily forecast and as today's max/min, until midnight.
    """

    def __init__(self) -> None:
        """Initialize the parser."""
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

    def extract_date(self, col: Tag) -> dt.date | None:
        """
        Return the date a daily column is for.

        The icon link's title holds it, e.g. "Péntek<br>2026. október 30.".
        """
        link = col.select_one(".dfIconAlert a[title]")
        title = link.get("title") if link else None
        if not isinstance(title, str):
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

    def parse(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Parse daily forecast data."""
        result = {}
        daily_forecast = []
        daily_cols = soup.find_all("div", class_="ik dailyForecastCol")
        today = dt.datetime.now(tz=TimeUtils.get_local_timezone()).date()

        # Each column carries its own date: in the evening idokep.hu drops
        # today's column, so the strip does not always start with today
        previous_date: dt.date | None = None
        for col in daily_cols:
            if not isinstance(col, Tag):
                continue

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

    def restore_today_forecast(self, item: dict[str, Any]) -> None:
        """Remember today's daily entry from before a restart."""
        self._today_item = item

    def parse_today_min_max(self, soup: BeautifulSoup) -> dict[str, int]:
        """
        Extract today's forecast max and min temperature.

        Taken from the daily column dated today, or from today's entry kept
        from earlier today once the page has dropped that column.
        """
        today = dt.datetime.now(tz=TimeUtils.get_local_timezone()).date()
        for col in soup.find_all("div", class_="ik dailyForecastCol"):
            if isinstance(col, Tag) and self.extract_date(col) == today:
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
        if self._today_item.get("datetime") != today.isoformat():
            self._today_item = None
            return None
        return self._today_item

    def _parse_daily_column(self, col: Tag, forecast_date: dt.date) -> dict[str, Any]:
        """Parse individual daily forecast column."""
        min_temp, max_temp = self.extract_temperatures(col)
        condition = self.extract_condition(col)
        precipitation = self.extract_precipitation(col)
        precipitation_probability = self.extract_precipitation_probability(col)

        return {
            "datetime": str(forecast_date),
            "temperature": max_temp,
            "templow": min_temp,
            "condition": condition,
            "precipitation": precipitation,
            "precipitation_probability": precipitation_probability,
        }

    def extract_temperatures(self, col: Tag) -> tuple[int | None, int | None]:
        """
        Extract min and max temperatures from column.

        Returns:
            tuple: (min_temp, max_temp)

        """
        # First check for min-max-close or min-max-closer div (when temps are close)
        close_div = col.find("div", class_=["ik min-max-close", "ik min-max-closer"])
        if close_div and isinstance(close_div, Tag):
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

    def extract_temperature(self, col: Tag, class_name: str) -> int | None:
        """Extract temperature from column."""
        temp_div = col.find("div", class_=class_name)
        if temp_div and isinstance(temp_div, Tag):
            temp_a = temp_div.find("a")
            if temp_a and isinstance(temp_a, Tag):
                match = re.search(r"(-?\d+)", temp_a.get_text(strip=True))
                if match:
                    return int(match.group(1))
        return None

    def extract_condition(self, col: Tag) -> str | None:
        """Extract weather condition from column."""
        icon_alert = col.find("div", class_="ik dfIconAlert")
        if not (icon_alert and isinstance(icon_alert, Tag)):
            return None

        a_tag = icon_alert.find("a")
        if not (a_tag and isinstance(a_tag, Tag)):
            return None

        popover = a_tag.get("data-bs-content")
        if not isinstance(popover, str):
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
                return WeatherConditionMapper.map_condition(condition_text)

        # Fallback: grab text immediately after any forecast icon img closing >
        text_match = re.search(r"forecastIcons/[^>]+>([^<]+)", popover)
        if text_match:
            return WeatherConditionMapper.map_condition(text_match.group(1).strip())

        return None

    def extract_precipitation(self, col: Tag) -> int:
        """Extract precipitation amount."""
        precip_span = col.find("span", class_="ik mm")
        if precip_span and isinstance(precip_span, Tag):
            precip_text = precip_span.text.strip()
            if precip_text:
                match = re.search(r"(\d+)", precip_text)
                if match:
                    return int(match.group(1))
        return 0

    def extract_precipitation_probability(self, col: Tag) -> int:
        """Extract precipitation probability."""
        # Look for percentage values
        for element in col.find_all(["span", "div", "a"], string=re.compile(r"\d+%")):
            if isinstance(element, Tag):
                percent_text = element.text.strip()
                if percent_text.endswith("%"):
                    try:
                        return int(percent_text[:-1])
                    except ValueError:
                        continue

        # Look for data attributes
        for element in col.find_all(["a", "div"], attrs={"data-bs-content": True}):
            if isinstance(element, Tag):
                content = element.get("data-bs-content")
                if isinstance(content, str) and (
                    "csapadék" in content.lower() or "precipitation" in content.lower()
                ):
                    percent_match = re.search(r"(\d+)%", content)
                    if percent_match:
                        try:
                            return int(percent_match.group(1))
                        except ValueError:
                            continue

        return 0


class IdokepApiClient:
    """Időkép client: one fetch method per idokep.hu page."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self._http_client = HttpClient(session)
        self._current_parser = CurrentWeatherParser()
        self._hourly_parser = HourlyForecastParser()
        self._daily_parser = DailyForecastParser()
        self._alert_parser = AlertParser()

    # Each method raises IdokepApiClientCommunicationError on network/HTTP
    # failure, so the caller can tell a failed fetch from a page without data.
    async def async_get_current_weather(self, location: str) -> dict[str, Any]:
        """Fetch and parse the current conditions page."""
        soup = await self._get_soup(IdokepConfig.get_current_weather_url(location))
        return self._current_parser.parse(soup)

    async def async_get_hourly_forecast(self, location: str) -> dict[str, Any]:
        """
        Fetch the forecast page and parse everything on it.

        Hourly forecast, alerts, daily forecast and today's max/min all come
        from this one download.
        """
        soup = await self._get_soup(IdokepConfig.get_hourly_forecast_url(location))
        return {
            **self._hourly_parser.parse(soup),
            **self._alert_parser.parse(soup),
            **self._daily_parser.parse(soup),
            **self._daily_parser.parse_today_min_max(soup),
        }

    def restore_today_forecast(self, item: dict[str, Any]) -> None:
        """Seed today's daily entry restored from disk (kept until midnight)."""
        self._daily_parser.restore_today_forecast(item)

    def restore_hourly_forecast(self, items: list[dict[str, Any]]) -> None:
        """Seed the hourly items restored from disk (keeps the current hour)."""
        self._hourly_parser.restore_hourly_forecast(items)

    async def _get_soup(self, url: str) -> BeautifulSoup:
        """Download a page and return it parsed."""
        html = await self._http_client.get_html(url)
        return BeautifulSoup(html, "html.parser")
