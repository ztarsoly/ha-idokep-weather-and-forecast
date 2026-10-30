"""Unit tests for the Időkép API client."""

from __future__ import annotations

import datetime as dt
import zoneinfo
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from bs4 import BeautifulSoup, Tag

from custom_components.idokep_weather_and_forecast.api import (
    _BROWSER_HEADERS,
    AlertData,
    AlertParser,
    DailyForecastParser,
    HourlyForecastParser,
    HttpClient,
    IdokepApiClient,
    IdokepApiClientCommunicationError,
    IdokepApiClientError,
    TimeUtils,
    WeatherConditionMapper,
    WindStrengthMapper,
)


class TestIdokepApiClientExceptions:
    """Test exception classes and helper functions."""

    def test_idokep_api_client_error_inheritance(self) -> None:
        """Test that IdokepApiClientError is an Exception."""
        error = IdokepApiClientError("test error")
        assert isinstance(error, Exception)
        assert str(error) == "test error"

    def test_communication_error_inheritance(self) -> None:
        """Test that IdokepApiClientCommunicationError inherits correctly."""
        error = IdokepApiClientCommunicationError("comm error")
        assert isinstance(error, IdokepApiClientError)
        assert isinstance(error, Exception)
        assert str(error) == "comm error"


class TestHttpClientBrowserHeaders:
    """
    Test that HttpClient sends browser-like headers to avoid 403 errors.

    idokep.hu returns HTTP 403 Forbidden for requests that use Home
    Assistant's default aiohttp User-Agent. These tests guard against a
    regression back to unauthenticated/bare requests.
    """

    @pytest.fixture
    def mock_session(self) -> Mock:
        """Create a mock aiohttp session."""
        return Mock(spec=aiohttp.ClientSession)

    def _make_get_context_manager(self, response: Mock) -> Mock:
        """Wrap a mock response in an async context manager."""
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        return response

    @pytest.mark.asyncio
    async def test_get_html_sends_browser_headers(self, mock_session: Mock) -> None:
        """Test get_html() passes browser headers to avoid 403 from idokep.hu."""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value="<html></html>")
        self._make_get_context_manager(mock_response)
        mock_session.get = Mock(return_value=mock_response)

        http_client = HttpClient(mock_session)
        result = await http_client.get_html("https://www.idokep.hu/idojaras/Test")

        assert result == "<html></html>"
        mock_session.get.assert_called_once_with(
            "https://www.idokep.hu/idojaras/Test", headers=_BROWSER_HEADERS
        )
        # Explicitly guard the User-Agent that fixed the 403 issue.
        assert "Mozilla" in _BROWSER_HEADERS["User-Agent"]

    @pytest.mark.asyncio
    async def test_get_html_403_raises_communication_error(
        self, mock_session: Mock
    ) -> None:
        """Test get_html() surfaces a 403 (Forbidden) as a communication error."""
        request_info = Mock()
        history: tuple = ()
        mock_session.get = Mock(
            side_effect=aiohttp.ClientResponseError(
                request_info=request_info,
                history=history,
                status=403,
                message="Forbidden",
            )
        )

        http_client = HttpClient(mock_session)

        with pytest.raises(IdokepApiClientCommunicationError) as exc_info:
            await http_client.get_html("https://www.idokep.hu/idojaras/Test")

        assert "403" in str(exc_info.value)


class TestIdokepApiClientZoneInfoHandling:
    """Test zoneinfo import error handling."""

    def test_zoneinfo_import_error_handling(self) -> None:
        """Test that the module handles zoneinfo import errors gracefully."""
        # This test ensures the try/except block for zoneinfo import is covered
        # The import is already done at module level, so we test the fallback behavior
        with patch("custom_components.idokep_weather_and_forecast.api.zoneinfo", None):
            tz = TimeUtils.get_local_timezone()
        assert tz.utcoffset(None) == dt.timedelta(hours=2)


class TestIdokepApiClient:
    """Test the main API client class."""

    @pytest.fixture
    def mock_session(self) -> Mock:
        """Create a mock aiohttp session."""
        return Mock(spec=aiohttp.ClientSession)

    @pytest.fixture
    def api_client(self, mock_session: Mock) -> IdokepApiClient:
        """Create an API client with mocked session."""
        return IdokepApiClient(mock_session)

    def test_init(self, mock_session: Mock) -> None:
        """Test API client initialization."""
        client = IdokepApiClient(mock_session)
        assert client._http_client._session is mock_session

    def test_map_condition_sunny(self) -> None:
        """Test condition mapping for sunny conditions."""
        assert WeatherConditionMapper.map_condition("napos") == "sunny"
        assert WeatherConditionMapper.map_condition("derült") == "sunny"
        assert (
            WeatherConditionMapper.map_condition("NAPOS") == "sunny"
        )  # Case insensitive

    def test_map_condition_cloudy(self) -> None:
        """Test condition mapping for cloudy conditions."""
        assert WeatherConditionMapper.map_condition("borult") == "cloudy"
        assert WeatherConditionMapper.map_condition("erősen felhős") == "cloudy"

    def test_map_condition_partly_cloudy(self) -> None:
        """Test condition mapping for partly cloudy conditions."""
        assert (
            WeatherConditionMapper.map_condition("közepesen felhős") == "partlycloudy"
        )
        assert WeatherConditionMapper.map_condition("gyengén felhős") == "partlycloudy"

    def test_map_condition_rainy(self) -> None:
        """Test condition mapping for rainy conditions."""
        assert WeatherConditionMapper.map_condition("zápor") == "rainy"
        assert WeatherConditionMapper.map_condition("szitálás") == "rainy"
        assert WeatherConditionMapper.map_condition("gyenge eső") == "rainy"
        assert WeatherConditionMapper.map_condition("eső") == "rainy"
        assert WeatherConditionMapper.map_condition("eső viharos széllel") == "rainy"

    def test_map_condition_special(self) -> None:
        """Test condition mapping for special conditions."""
        assert WeatherConditionMapper.map_condition("zivatar") == "lightning-rainy"
        assert WeatherConditionMapper.map_condition("erős eső") == "pouring"
        assert WeatherConditionMapper.map_condition("jégeső") == "hail"
        assert WeatherConditionMapper.map_condition("havazás") == "snowy"
        assert WeatherConditionMapper.map_condition("havas eső") == "snowy-rainy"
        assert WeatherConditionMapper.map_condition("köd") == "fog"
        assert WeatherConditionMapper.map_condition("villámlás") == "lightning"
        assert WeatherConditionMapper.map_condition("szeles") == "windy"

    def test_map_condition_unknown(self) -> None:
        """Test condition mapping for unknown conditions."""
        assert WeatherConditionMapper.map_condition("ismeretlen időjárás") == "unknown"
        assert WeatherConditionMapper.map_condition("") == "unknown"


class TestIdokepApiClientWeatherScraping:
    """Test weather data scraping functionality."""

    @pytest.fixture
    def mock_session(self) -> Mock:
        """Create a mock aiohttp session."""
        return Mock(spec=aiohttp.ClientSession)

    @pytest.fixture
    def api_client(self, mock_session: Mock) -> IdokepApiClient:
        """Create an API client with mocked session."""
        return IdokepApiClient(mock_session)

    @pytest.fixture
    def sample_current_weather_html(self) -> str:
        """Sample HTML for current weather page."""
        return """
        <html>
            <div class="ik current-temperature">22°C</div>
            <div class="ik current-weather">Napos</div>
            <div class="ik current-weather-title">Jelenleg</div>
            <div class="ik current-weather-short-desc">
                Kellemes idő várható ma.
            </div>
            <div>
                <img alt="Napkelte 06:30" />
                Napkelte 06:30
            </div>
            <div>
                <img alt="Napnyugta 19:45" />
                Napnyugta 19:45
            </div>
            <span class="ik mm">2 mm</span>
            <div>Csapadék esélye: 30%</div>
        </html>
        """

    @pytest.fixture
    def sample_hourly_forecast_html(self) -> str:
        """
        Sample HTML for hourly forecast page.

        Covers 36 hours: Day 1 (0-23h) + Day 2 (0-11h).
        """
        # Generate 24 hours for Day 1 (0-23)
        day1_cards = []
        for hour in range(24):
            temp = 10 - hour  # Temperature decreasing through the day
            condition = "Napos" if hour < 12 else "Borult"
            rain_chance = 10 + (hour * 2)
            rain_level = 3 if hour < 12 else 5
            day1_cards.append(f"""
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">{hour:02d}:00</div>
                <div class="ik tempValue"><a>{temp}</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="{condition}"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>{rain_chance}%</a></div>
                <div class="ik rainlevel" style="height: {rain_level}px;"></div>
            </div>""")

        # Generate 12 hours for Day 2 (0-11)
        day2_cards = []
        for hour in range(12):
            temp = -5 - hour  # Colder second day
            condition = "Havazás" if hour < 6 else "Hófúvás"
            rain_chance = 50 + (hour * 3)
            rain_level = 7 if hour < 6 else 9
            day2_cards.append(f"""
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">{hour:02d}:00</div>
                <div class="ik tempValue"><a>{temp}</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="{condition}"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>{rain_chance}%</a></div>
                <div class="ik rainlevel" style="height: {rain_level}px;"></div>
            </div>""")

        all_cards = "".join(day1_cards + day2_cards)
        return f"""
        <html>
            {all_cards}
        </html>
        """

    @pytest.fixture
    def sample_daily_forecast_html(self) -> str:
        """Sample HTML for daily forecast page."""
        return """
        <html>
            <div class="ik dailyForecastCol">
                <div class="ik min-max-container">
                    <div class="ik max"><a>25</a></div>
                    <div class="ik min"><a>15</a></div>
                </div>
                <div class="ik dfIconAlert">
                    <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/010.svg' alt='Napos'&gt;"></a>
                </div>
                <span class="ik mm">3 mm</span>
                <span>15%</span>
            </div>
            <div class="ik dailyForecastCol">
                <div class="ik min-max-container">
                    <div class="ik max"><a>20</a></div>
                    <div class="ik min"><a>10</a></div>
                </div>
                <div class="ik dfIconAlert">
                    <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/082.svg' alt='Eső'&gt;"></a>
                </div>
                <span class="ik mm">8 mm</span>
                <span>80%</span>
            </div>
            <div class="ik dailyForecastCol">
                <div class="ik min-max-container">
                    <div class="ik min-max-close">
                        <a>0</a>
                        <a>-2</a>
                    </div>
                </div>
                <div class="ik dfIconAlert">
                    <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/060.svg' alt='Havazás'&gt;"></a>
                </div>
                <span class="ik mm">2 cm</span>
                <span>5%</span>
            </div>
            <div class="ik dailyForecastCol">
                <div class="ik min-max-container">
                    <div class="ik min-max-closer">
                        <a>-3</a>
                        <a>-4</a>
                    </div>
                </div>
                <div class="ik dfIconAlert">
                    <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/072.svg' alt='Hófúvás'&gt;"></a>
                </div>
                <span class="ik mm">9 cm</span>
                <span>0%</span>
            </div>
        </html>
        """

    @pytest.mark.asyncio
    async def test_scrape_current_weather_success(
        self,
        api_client: IdokepApiClient,
        mock_session: Mock,
        sample_current_weather_html: str,
    ) -> None:
        """Test successful current weather scraping."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value=sample_current_weather_html)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.get = Mock(return_value=mock_response)

        with patch(
            "custom_components.idokep_weather_and_forecast.api.async_timeout.timeout"
        ):
            result = await api_client.async_get_current_weather("Budapest")

        assert set(result) == {"temperature", "condition"}
        assert result["temperature"] == 22
        assert result["condition"] == "sunny"

    @pytest.mark.asyncio
    async def test_scrape_hourly_forecast_success(
        self,
        api_client: IdokepApiClient,
        mock_session: Mock,
        sample_hourly_forecast_html: str,
    ) -> None:
        """Test successful hourly forecast scraping with 36 hours."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value=sample_hourly_forecast_html)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.get = Mock(return_value=mock_response)

        with patch(
            "custom_components.idokep_weather_and_forecast.api.async_timeout.timeout"
        ):
            result = await api_client.async_get_hourly_forecast("Budapest")

        assert "hourly_forecast" in result
        forecast = result["hourly_forecast"]
        assert len(forecast) == 36, "Should have 36 hourly forecast items"

        # Verify forecast is sorted chronologically
        datetimes = [item["datetime"] for item in forecast]
        assert datetimes == sorted(datetimes), (
            "Forecast should be in chronological order"
        )

        # Verify first hour (Day 1, 00:00)
        first_item = forecast[0]
        assert first_item["temperature"] == 10
        assert first_item["condition"] == "sunny"
        assert first_item["precipitation_probability"] == 10
        assert first_item["precipitation"] == 3

        # Verify last hour of Day 1 (23:00)
        day1_last = forecast[23]
        assert day1_last["temperature"] == 10 - 23
        assert day1_last["condition"] == "cloudy"
        assert day1_last["precipitation_probability"] == 10 + (23 * 2)
        assert day1_last["precipitation"] == 5

        # Verify first hour of Day 2 (00:00, item 24)
        day2_first = forecast[24]
        assert day2_first["temperature"] == -5
        assert day2_first["condition"] == "snowy"
        assert day2_first["precipitation_probability"] == 50
        assert day2_first["precipitation"] == 7

        # Verify last hour (Day 2, 11:00)
        last_item = forecast[35]
        assert last_item["temperature"] == -5 - 11
        assert last_item["condition"] == "snowy"
        assert last_item["precipitation_probability"] == 50 + (11 * 3)
        assert last_item["precipitation"] == 9

        # Verify day transition: Day 2's 00:00 should be after Day 1's 23:00
        day1_dt = datetime.fromisoformat(day1_last["datetime"])
        day2_dt = datetime.fromisoformat(day2_first["datetime"])
        assert day2_dt > day1_dt
        assert (day2_dt - day1_dt).total_seconds() == 3600

    @pytest.mark.asyncio
    async def test_scrape_hourly_forecast_day_transition(
        self,
        api_client: IdokepApiClient,
        mock_session: Mock,
    ) -> None:
        """Test hourly forecast correctly handles day transitions."""
        # Create HTML with explicit day transition: 22:00, 23:00, 00:00, 01:00
        html = """
        <html>
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">22:00</div>
                <div class="ik tempValue"><a>5</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="Napos"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>10%</a></div>
                <div class="ik rainlevel" style="height: 3px;"></div>
            </div>
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">23:00</div>
                <div class="ik tempValue"><a>4</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="Napos"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>15%</a></div>
                <div class="ik rainlevel" style="height: 3px;"></div>
            </div>
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">00:00</div>
                <div class="ik tempValue"><a>3</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="Borult"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>20%</a></div>
                <div class="ik rainlevel" style="height: 5px;"></div>
            </div>
            <div class="ik wide-hourly-forecast-card">
                <div class="ik wide-hourly-forecast-hour">01:00</div>
                <div class="ik tempValue"><a>2</a></div>
                <div class="forecast-icon-container">
                    <a data-bs-content="Borult"></a>
                </div>
                <div class="ik hourly-rain-chance"><a>25%</a></div>
                <div class="ik rainlevel" style="height: 5px;"></div>
            </div>
        </html>
        """

        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value=html)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.get = Mock(return_value=mock_response)

        with patch(
            "custom_components.idokep_weather_and_forecast.api.async_timeout.timeout"
        ):
            result = await api_client.async_get_hourly_forecast("Budapest")

        forecast = result["hourly_forecast"]
        assert len(forecast) == 4

        # Check chronological order
        dt0 = datetime.fromisoformat(forecast[0]["datetime"])
        dt1 = datetime.fromisoformat(forecast[1]["datetime"])
        dt2 = datetime.fromisoformat(forecast[2]["datetime"])
        dt3 = datetime.fromisoformat(forecast[3]["datetime"])

        assert dt0.hour == 22
        assert dt1.hour == 23
        assert dt2.hour == 0
        assert dt3.hour == 1

        # Verify day transition: 00:00 should be on next day after 23:00
        assert dt2.date() > dt1.date()
        assert (dt2 - dt1).total_seconds() == 3600

    @pytest.mark.asyncio
    async def test_scrape_daily_forecast_success(
        self,
        api_client: IdokepApiClient,
        mock_session: Mock,
        sample_daily_forecast_html: str,
    ) -> None:
        """The daily columns on the forecast page give the daily forecast."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value=sample_daily_forecast_html)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.get = Mock(return_value=mock_response)

        with patch(
            "custom_components.idokep_weather_and_forecast.api.async_timeout.timeout"
        ):
            result = await api_client.async_get_hourly_forecast("Budapest")

        assert "daily_forecast" in result
        forecast = result["daily_forecast"]
        assert len(forecast) == 4

        first_day = forecast[0]
        assert first_day["temperature"] == 25  # max temp
        assert first_day["templow"] == 15  # min temp

        second_day = forecast[1]
        assert second_day["temperature"] == 20  # max temp
        assert second_day["templow"] == 10  # min temp

        # Test min-max-close structure (close temperatures)
        third_day = forecast[2]
        assert third_day["temperature"] == 0  # max temp
        assert third_day["templow"] == -2  # min temp

        # Test min-max-closer structure (very close temperatures)
        fourth_day = forecast[3]
        assert fourth_day["temperature"] == -3  # max temp
        assert fourth_day["templow"] == -4  # min temp
        assert first_day["condition"] == "sunny"
        assert first_day["precipitation"] == 3

        second_day = forecast[1]
        assert second_day["condition"] == "rainy"
        assert second_day["precipitation"] == 8

    def test_extract_hourly_precipitation_data(
        self, api_client: IdokepApiClient
    ) -> None:
        """Test hourly precipitation data extraction."""
        html = """
        <div class="card">
            <div class="ik hourly-rain-chance"><a>25%</a></div>
            <div class="ik rainlevel" style="height: 3px;"></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="card")

        if isinstance(card, Tag):
            precipitation, precipitation_probability = (
                api_client._hourly_parser.extract_precipitation_data(card)
            )
            assert precipitation == 3
            assert precipitation_probability == 25

    def test_extract_daily_temperature(self, api_client: IdokepApiClient) -> None:
        """Test daily temperature extraction."""
        html = """
        <div class="col">
            <div class="ik min"><a>-5</a></div>
            <div class="ik max"><a>15</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")

        if isinstance(col, Tag):
            min_temp = api_client._daily_parser.extract_temperature(col, "ik min")
            max_temp = api_client._daily_parser.extract_temperature(col, "ik max")
            assert min_temp == -5
            assert max_temp == 15

    def test_extract_daily_condition(self, api_client: IdokepApiClient) -> None:
        """Test daily condition extraction."""
        html = """
        <div class="col">
            <div class="ik dfIconAlert">
                <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/082.svg' alt='Eső viharos széllel'&gt;">
                </a>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")

        if isinstance(col, Tag):
            condition = api_client._daily_parser.extract_condition(col)
            assert condition == "rainy"

    def test_extract_daily_condition_fallback_match(
        self, api_client: IdokepApiClient
    ) -> None:
        """Test daily condition extraction using fallback regex match."""
        # This tests lines 445-449 (fallback match without closing bracket)
        html = """
        <div class="col">
            <div class="ik dfIconAlert">
                <a data-bs-content="&lt;img class='ik popover-icon' src='/assets/forecastIcons/010.svg' alt='Napos'&gt;">
                </a>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")

        if isinstance(col, Tag):
            condition = api_client._daily_parser.extract_condition(col)
            assert condition == "sunny"


# ---------------------------------------------------------------------------
# AlertParser tests
# ---------------------------------------------------------------------------


class TestAlertParserGeneral:
    """Tests for AlertParser._parse_general_alert()."""

    @pytest.fixture
    def parser(self) -> AlertParser:
        """Return a fresh AlertParser instance."""
        return AlertParser()

    def test_parse_returns_empty_alerts_when_no_topalertbar(
        self, parser: AlertParser
    ) -> None:
        """parse() returns empty alerts dict when there is no alert bar."""
        soup = BeautifulSoup("<div>no alerts here</div>", "html.parser")
        result = parser.parse(soup)
        assert result["alerts"] == []
        assert "alerts_by_level" in result

    def test_parse_general_alert_yellow(self, parser: AlertParser) -> None:
        """Yellow topalertbar produces a yellow-level alert."""
        html = """
        <div id="topalertbar" class="yellow">
            <a>riasztás vihar miatt</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser.parse(soup)
        assert len(result["alerts"]) == 1
        alert = result["alerts"][0]
        assert alert.level == "yellow"
        assert alert.type == "storm"

    def test_parse_general_alert_orange(self, parser: AlertParser) -> None:
        """Orange topalertbar produces an orange-level alert."""
        html = """
        <div id="topalertbar" class="orange">
            <a>riasztás szél miatt</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser.parse(soup)
        assert len(result["alerts"]) == 1
        assert result["alerts"][0].level == "orange"
        assert result["alerts"][0].type == "wind"

    def test_parse_general_alert_red(self, parser: AlertParser) -> None:
        """Red topalertbar produces a red-level alert."""
        html = """
        <div id="topalertbar" class="red">
            <a>riasztás hó miatt</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser.parse(soup)
        assert len(result["alerts"]) == 1
        assert result["alerts"][0].level == "red"
        assert result["alerts"][0].type == "snow"

    def test_parse_general_alert_no_level_class(self, parser: AlertParser) -> None:
        """Alert bar without a recognised level class produces no alerts."""
        html = """
        <div id="topalertbar" class="info">
            <a>some alert text</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser.parse(soup)
        assert result["alerts"] == []

    def test_parse_general_alert_no_link(self, parser: AlertParser) -> None:
        """Alert bar without an anchor tag produces no alerts."""
        html = """
        <div id="topalertbar" class="yellow">
            <span>riasztás</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser.parse(soup)
        assert result["alerts"] == []

    def test_alerts_by_level_structure(self, parser: AlertParser) -> None:
        """alerts_by_level always has yellow/orange/red keys."""
        soup = BeautifulSoup("", "html.parser")
        result = parser.parse(soup)
        for key in ("yellow", "orange", "red"):
            assert key in result["alerts_by_level"]
            assert isinstance(result["alerts_by_level"][key], list)


class TestAlertParserHourly:
    """Tests for AlertParser._parse_hourly_alerts()."""

    @pytest.fixture
    def parser(self) -> AlertParser:
        """Return a fresh AlertParser instance."""
        return AlertParser()

    def test_hourly_alert_yellow_sarga(self, parser: AlertParser) -> None:
        """'Sárga' in description → yellow level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Sárga riasztás szél miatt">
                <img class="forecast-alert-icon" src="/icons/wind.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0].level == "yellow"
        assert result[0].type == "wind"

    def test_hourly_alert_orange_narancs(self, parser: AlertParser) -> None:
        """'Narancs' in description → orange level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Narancs riasztás eső miatt">
                <img class="forecast-alert-icon" src="/icons/rain.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0].level == "orange"
        assert result[0].type == "rain"

    def test_hourly_alert_red_piros(self, parser: AlertParser) -> None:
        """'Piros' in description → red level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Piros riasztás hóvihar miatt">
                <img class="forecast-alert-icon" src="/icons/snow.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0].level == "red"

    def test_hourly_alert_red_voros(self, parser: AlertParser) -> None:
        """'Vörös' in description → red level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Vörös riasztás vihar miatt">
                <img class="forecast-alert-icon" src="https://cdn.example.com/alert.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0].level == "red"

    def test_hourly_alert_no_recognised_level(self, parser: AlertParser) -> None:
        """Description without a recognised level keyword → no alert."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Unknown alert text">
                <img class="forecast-alert-icon" src="/icons/alert.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_no_hover_over_link(self, parser: AlertParser) -> None:
        """Container without a hover-over link → no alert."""
        html = """
        <div class="genericHourlyAlert">
            <span>Sárga riasztás</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_empty_description(self, parser: AlertParser) -> None:
        """Empty data-bs-content → no alert."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="">
                <img class="forecast-alert-icon" src="/icons/alert.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_no_containers(self, parser: AlertParser) -> None:
        """No genericHourlyAlert divs → empty list."""
        soup = BeautifulSoup("<div>nothing</div>", "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_icon_url_leading_slash_prepends_base(
        self, parser: AlertParser
    ) -> None:
        """Icon src starting with '/' gets the idokep base URL prepended."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Sárga riasztás szél miatt">
                <img class="forecast-alert-icon" src="/icons/wind.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result[0].icon_url == "https://www.idokep.hu/icons/wind.png"

    def test_hourly_alert_icon_url_full_url_unchanged(
        self, parser: AlertParser
    ) -> None:
        """Icon src that is already a full URL is kept as-is."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Sárga riasztás szél miatt">
                <img class="forecast-alert-icon" src="https://cdn.example.com/wind.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert result[0].icon_url == "https://cdn.example.com/wind.png"

    def test_hourly_alert_deduplication(self, parser: AlertParser) -> None:
        """Identical alerts appearing twice are deduplicated."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Sárga riasztás szél miatt">
                <img class="forecast-alert-icon" src="/icons/wind.png" />
            </a>
        </div>
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Sárga riasztás szél miatt">
                <img class="forecast-alert-icon" src="/icons/wind.png" />
            </a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1


class TestAlertParserHelpers:
    """Tests for _extract_alert_type and _organize_by_level."""

    @pytest.fixture
    def parser(self) -> AlertParser:
        """Return a fresh AlertParser instance."""
        return AlertParser()

    @pytest.mark.parametrize(
        ("description", "expected_type"),
        [
            ("ónos eső figyelmeztetés", "freezing_rain"),
            ("ónosesőre vonatkozó riasztás", "freezing_rain"),
            ("vihar miatt", "storm"),
            ("zivatar várható", "thunderstorm"),
            ("szél riasztás", "wind"),
            ("hó riasztás", "snow"),
            ("eső riasztás", "rain"),
            ("köd figyelmeztetés", "fog"),
            ("hőség riasztás", "heat"),
            ("hideg riasztás", "cold"),
            ("fagy riasztás", "frost"),
        ],
    )
    def test_extract_alert_type_known(
        self, parser: AlertParser, description: str, expected_type: str
    ) -> None:
        """Known Hungarian keywords map to the correct English type."""
        assert parser._extract_alert_type(description) == expected_type

    def test_extract_alert_type_unknown_falls_back_to_general(
        self, parser: AlertParser
    ) -> None:
        """Unrecognised description falls back to 'general'."""
        assert parser._extract_alert_type("unknown weather event") == "general"

    def test_organize_by_level(self, parser: AlertParser) -> None:
        """Alerts are correctly grouped by severity level."""
        alerts = [
            AlertData(level="yellow", type="wind", description="szél", icon_url=None),
            AlertData(level="red", type="storm", description="vihar", icon_url=None),
            AlertData(level="yellow", type="rain", description="eső", icon_url=None),
        ]
        result = parser._organize_by_level(alerts)
        assert len(result["yellow"]) == 2
        assert len(result["red"]) == 1
        assert len(result["orange"]) == 0


# ---------------------------------------------------------------------------
# Short forecast new HTML structure (lines 335-339)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HourlyForecastParser branch tests
# ---------------------------------------------------------------------------


class TestHourlyParserBranches:
    """Tests for uncovered HourlyForecastParser branches."""

    @pytest.fixture
    def parser(self) -> HourlyForecastParser:
        """Return a HourlyForecastParser instance."""
        return HourlyForecastParser()

    # ---- _parse_hourly_card ------------------------------------------------

    def test_parse_hourly_card_hour_without_colon(
        self, parser: HourlyForecastParser
    ) -> None:
        """Hour text without colon uses 0 for minutes (covers the else 0 branch)."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">14</div>
            <div class="ik tempValue"><a>20</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        hour_div = card.find("div", class_="ik wide-hourly-forecast-hour")
        result = parser._parse_hourly_card(card, hour_div, dt.date(2025, 6, 1))
        assert result is not None
        assert result["datetime"].startswith("2025-06-01T14:00")

    def test_parse_hourly_card_invalid_hour_returns_none(
        self, parser: HourlyForecastParser
    ) -> None:
        """Non-integer hour_text triggers ValueError and returns None."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">XX:00</div>
            <div class="ik tempValue"><a>20</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        hour_div = card.find("div", class_="ik wide-hourly-forecast-hour")
        result = parser._parse_hourly_card(card, hour_div, dt.date(2025, 6, 1))
        assert result is None

    def test_parse_hourly_card_datetime_is_budapest_aware(
        self, parser: HourlyForecastParser
    ) -> None:
        """Hourly datetimes carry the Budapest UTC offset."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">14:00</div>
            <div class="ik tempValue"><a>20</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        hour_div = card.find("div", class_="ik wide-hourly-forecast-hour")
        result = parser._parse_hourly_card(card, hour_div, dt.date(2025, 6, 1))
        assert result is not None
        # Summer time in Budapest is UTC+2
        assert result["datetime"] == "2025-06-01T14:00:00+02:00"

    def test_parse_skips_card_with_non_numeric_hour(
        self, parser: HourlyForecastParser
    ) -> None:
        """A card labelled e.g. 'Most' is skipped instead of failing the parse."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">Most</div>
            <div class="ik tempValue"><a>19</a></div>
        </div>
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">15:00</div>
            <div class="ik tempValue"><a>20</a></div>
        </div>
        """
        result = parser.parse(BeautifulSoup(html, "html.parser"))
        forecast = result["hourly_forecast"]
        assert len(forecast) == 1
        assert datetime.fromisoformat(forecast[0]["datetime"]).hour == 15

    # ---- wind bearing --------------------------------------------------------

    # Card markup as served by idokep.hu (2026-10-29)
    WIND_CARD_HTML = """
    <div class="ik wide-hourly-forecast-card">
        <div class="ik wide-hourly-forecast-hour">{hour}</div>
        <div class="ik forecast-icon-container">
            <a class="ik d-block w-100 hover-over" data-bs-content="erősen felhős"></a>
        </div>
        <div class="ik genericHourlyAlert"> </div>
        <div class="ik tempBarGraph">
            <div class="ik tempValue"><a class="ik hover-over d-block"> 13 </a></div>
        </div>
        <div class="ik hourly-wind">
            <a class="ik hover-over d-block"
               data-bs-content="Mérsékelt északi szél">
                <div class="ik wind mersekelt" style="--rotateAngle:{angle}deg"></div>
            </a>
        </div>
    </div>
    """

    def test_extract_wind_bearing(self, parser: HourlyForecastParser) -> None:
        """Bearing is read from the arrow's --rotateAngle style."""
        html = self.WIND_CARD_HTML.format(hour="16:00", angle=6)
        card = BeautifulSoup(html, "html.parser").find("div")
        assert parser.extract_wind_bearing(card) == 6

    def test_extract_wind_bearing_normalises_to_0_359(
        self, parser: HourlyForecastParser
    ) -> None:
        """Angles outside 0-359 are wrapped."""
        html = self.WIND_CARD_HTML.format(hour="16:00", angle=370)
        card = BeautifulSoup(html, "html.parser").find("div")
        assert parser.extract_wind_bearing(card) == 10

    def test_extract_wind_bearing_missing(self, parser: HourlyForecastParser) -> None:
        """No wind element or no angle gives None."""
        no_wind = BeautifulSoup("<div class='card'></div>", "html.parser").find("div")
        assert parser.extract_wind_bearing(no_wind) is None

        no_angle = BeautifulSoup(
            "<div><div class='ik hourly-wind'><div class='ik wind gyenge'></div>"
            "</div></div>",
            "html.parser",
        ).find("div")
        assert parser.extract_wind_bearing(no_angle) is None

    def test_parse_adds_wind_bearing_to_items_and_current_hour(
        self, parser: HourlyForecastParser
    ) -> None:
        """Each hourly item gets wind_bearing; the current slot sets the top level."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        fixed_now = dt.datetime(2026, 10, 29, 16, 20, tzinfo=local_tz)

        class FrozenDateTime(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return fixed_now.astimezone(tz)

        html = self.WIND_CARD_HTML.format(
            hour="16:00", angle=6
        ) + self.WIND_CARD_HTML.format(hour="17:00", angle=200)
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            result = parser.parse(BeautifulSoup(html, "html.parser"))

        bearings = [item["wind_bearing"] for item in result["hourly_forecast"]]
        # Both cards say "mersekelt": moderate breeze, estimated 24 km/h
        assert result["wind_strength"] == "moderate_breeze"
        assert result["wind_speed"] == 24
        for item in result["hourly_forecast"]:
            assert item["wind_strength"] == "moderate_breeze"
            assert item["native_wind_speed"] == 24
        assert bearings == [6, 200]
        assert result["wind_bearing"] == 6

    def test_current_hour_falls_back_to_first_slot(self) -> None:
        """If no slot contains now, the first forecast item is used."""
        forecast = [
            {"datetime": "2026-10-29T17:00:00+01:00", "wind_bearing": 90},
            {"datetime": "2026-10-29T18:00:00+01:00", "wind_bearing": 180},
        ]
        now = dt.datetime(
            2026, 10, 29, 16, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))
        )
        item = HourlyForecastParser._current_hour_item(forecast, now)
        assert item["wind_bearing"] == 90

    def _parse_at(
        self, parser: HourlyForecastParser, now: dt.datetime, hours: list[str]
    ) -> dict:
        """Parse wind cards for the given hours with the clock frozen at now."""

        class FrozenDateTime(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return now.astimezone(tz)

        html = "".join(
            self.WIND_CARD_HTML.format(hour=hour, angle=90 * index)
            for index, hour in enumerate(hours)
        )
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            return parser.parse(BeautifulSoup(html, "html.parser"))

    def test_parse_keeps_current_hour_dropped_by_page(
        self, parser: HourlyForecastParser
    ) -> None:
        """The page starts at the next hour; the earlier seen current hour stays first."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        self._parse_at(
            parser,
            dt.datetime(2026, 10, 30, 11, 0, tzinfo=local_tz),
            ["12:00", "13:00"],
        )
        result = self._parse_at(
            parser,
            dt.datetime(2026, 10, 30, 12, 51, tzinfo=local_tz),
            ["13:00", "14:00"],
        )

        hours = [item["datetime"][11:16] for item in result["hourly_forecast"]]
        assert hours == ["12:00", "13:00", "14:00"]
        # Current wind comes from the kept 12:00 slot (first card, 0 degrees)
        assert result["wind_bearing"] == 0

    def test_parse_drops_kept_hour_once_over(
        self, parser: HourlyForecastParser
    ) -> None:
        """A kept hour is not put back after it has ended."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        self._parse_at(
            parser,
            dt.datetime(2026, 10, 30, 11, 0, tzinfo=local_tz),
            ["12:00", "13:00"],
        )
        result = self._parse_at(
            parser,
            dt.datetime(2026, 10, 30, 13, 5, tzinfo=local_tz),
            ["14:00", "15:00"],
        )

        hours = [item["datetime"][11:16] for item in result["hourly_forecast"]]
        assert hours == ["13:00", "14:00", "15:00"]

    def test_parse_keeps_restored_current_hour(
        self, parser: HourlyForecastParser
    ) -> None:
        """Hourly items restored from disk also provide the current hour."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        parser.restore_hourly_forecast(
            [{"datetime": "2026-10-30T12:00:00+01:00", "temperature": 13}]
        )
        result = self._parse_at(
            parser, dt.datetime(2026, 10, 30, 12, 51, tzinfo=local_tz), ["13:00"]
        )

        assert result["hourly_forecast"][0] == {
            "datetime": "2026-10-30T12:00:00+01:00",
            "temperature": 13,
        }
        assert len(result["hourly_forecast"]) == 2

    def test_parse_without_earlier_data_starts_at_page(
        self, parser: HourlyForecastParser
    ) -> None:
        """With nothing seen before, the forecast starts where the page does."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        result = self._parse_at(
            parser, dt.datetime(2026, 10, 30, 12, 51, tzinfo=local_tz), ["13:00"]
        )

        assert [item["datetime"][11:16] for item in result["hourly_forecast"]] == [
            "13:00"
        ]

    # ---- extract_precipitation_probability ---------------------------------

    def test_precipitation_probability_no_rain_chance_div(
        self, parser: HourlyForecastParser
    ) -> None:
        """No hourly-rain-chance div → returns 0."""
        html = "<div class='ik wide-hourly-forecast-card'></div>"
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div")
        assert parser.extract_precipitation_probability(card) == 0

    def test_precipitation_probability_no_anchor(
        self, parser: HourlyForecastParser
    ) -> None:
        """hourly-rain-chance div with no <a> → returns 0."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik hourly-rain-chance"><span>50%</span></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_probability(card) == 0

    def test_precipitation_probability_no_percent_sign(
        self, parser: HourlyForecastParser
    ) -> None:
        """<a> text that doesn't end with '%' → returns 0."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik hourly-rain-chance"><a>50 mm</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_probability(card) == 0

    def test_precipitation_probability_value_error(
        self, parser: HourlyForecastParser
    ) -> None:
        """<a> text ending with '%' but non-int prefix → returns 0."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik hourly-rain-chance"><a>abc%</a></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_probability(card) == 0

    # ---- extract_precipitation_amount -------------------------------------

    def test_precipitation_amount_rainlevel_na_returns_zero(
        self, parser: HourlyForecastParser
    ) -> None:
        """rainlevel-na → returns 0."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik rainlevel-na"></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_amount(card) == 0

    def test_precipitation_amount_no_rainlevel_returns_zero(
        self, parser: HourlyForecastParser
    ) -> None:
        """No rainlevel div at all → returns 0."""
        html = "<div class='ik wide-hourly-forecast-card'></div>"
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div")
        assert parser.extract_precipitation_amount(card) == 0

    def test_precipitation_amount_rainlevel_no_height_returns_one(
        self, parser: HourlyForecastParser
    ) -> None:
        """Rainlevel div without a height style returns 1 (some rain)."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik rainlevel"></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_amount(card) == 1


# ---------------------------------------------------------------------------
# DailyForecastParser branch tests
# ---------------------------------------------------------------------------


class TestDailyParserBranches:
    """Tests for uncovered DailyForecastParser branches."""

    @pytest.fixture
    def parser(self) -> DailyForecastParser:
        """Return a DailyForecastParser instance."""
        return DailyForecastParser()

    def test_parse_uses_budapest_date_not_utc(
        self, parser: DailyForecastParser
    ) -> None:
        """Just after local midnight the first day is the Budapest date."""
        # 23:30 UTC on June 1st is already 01:30 on June 2nd in Budapest
        fixed_utc = dt.datetime(2025, 6, 1, 23, 30, tzinfo=dt.UTC)

        class FrozenDateTime(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return fixed_utc.astimezone(tz)

        html = '<div class="ik dailyForecastCol"></div>'
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            result = parser.parse(BeautifulSoup(html, "html.parser"))
        assert result["daily_forecast"][0]["datetime"] == "2025-06-02"

    # ---- extract_temperature -----------------------------------------------

    def test_extract_temperature_no_match_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """Return None when the <a> tag contains no numeric text."""
        # Build a parent col div so find works correctly
        container = BeautifulSoup(
            '<div class="col"><div class="ik max"><a>no numbers</a></div></div>',
            "html.parser",
        ).find("div", class_="col")
        result = parser.extract_temperature(container, "ik max")
        assert result is None

    # ---- extract_condition -------------------------------------------------

    def test_extract_condition_no_df_icon_alert_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """No dfIconAlert div → returns None."""
        html = "<div class='col'></div>"
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div")
        assert parser.extract_condition(col) is None

    def test_extract_condition_no_a_tag_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """Column with dfIconAlert but no anchor returns None."""
        html = """
        <div class="col">
            <div class="ik dfIconAlert"><span>icon</span></div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        assert parser.extract_condition(col) is None

    def test_extract_condition_non_string_popover_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """<a> without data-bs-content attribute → returns None."""
        html = """
        <div class="col">
            <div class="ik dfIconAlert">
                <a href="#">icon</a>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        assert parser.extract_condition(col) is None

    def test_extract_condition_fallback_text_match(
        self, parser: DailyForecastParser
    ) -> None:
        """When alt regex fails but the text-after->  regex matches, use that."""
        # Popover has forecastIcons path but instead of alt= uses just img text
        html = """
        <div class="col">
            <div class="ik dfIconAlert">
                <a data-bs-content="&lt;img src='forecastIcons/sunny.png'&gt;napos">icon</a>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        result = parser.extract_condition(col)
        # napos → sunny
        assert result == "sunny"

    def test_extract_condition_no_match_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """Popover with no forecastIcons reference → returns None."""
        html = """
        <div class="col">
            <div class="ik dfIconAlert">
                <a data-bs-content="no forecast icon here">icon</a>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        assert parser.extract_condition(col) is None

    # ---- extract_precipitation_probability (data-attribute path) -----------

    def test_extract_precip_probability_data_attribute_csapadek(
        self, parser: DailyForecastParser
    ) -> None:
        """data-bs-content containing 'csapadék' and a percent → extracts value."""
        html = """
        <div class="col">
            <a data-bs-content="Csapadék valószínűsége: 40%">info</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        result = parser.extract_precipitation_probability(col)
        assert result == 40

    def test_extract_precip_probability_data_attribute_precipitation(
        self, parser: DailyForecastParser
    ) -> None:
        """data-bs-content containing 'precipitation' and a percent → extracts value."""
        html = """
        <div class="col">
            <a data-bs-content="Precipitation probability: 65%">info</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        result = parser.extract_precipitation_probability(col)
        assert result == 65

    def test_extract_precip_probability_data_attribute_returns_zero_when_none(
        self, parser: DailyForecastParser
    ) -> None:
        """No matching data-bs-content → returns 0."""
        html = """
        <div class="col">
            <a data-bs-content="Temperature: 25C">info</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        result = parser.extract_precipitation_probability(col)
        assert result == 0

    def test_extract_precipitation_no_span_returns_zero(
        self, parser: DailyForecastParser
    ) -> None:
        """No ik mm span → extract_precipitation returns 0."""
        html = "<div class='col'></div>"
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div")
        assert parser.extract_precipitation(col) == 0

    def test_extract_precip_probability_value_error_in_string_loop(
        self, parser: DailyForecastParser
    ) -> None:
        r"""
        Text matching \d+% search but non-integer before % triggers ValueError->0.

        "50.5%" matches the \d+% regex via search (matches "0.5%" from index 3).
        The full text ends with %, so int("50.5") raises ValueError -> continue.
        """
        html = """
        <div class="col">
            <span>50.5%</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        col = soup.find("div", class_="col")
        result = parser.extract_precipitation_probability(col)
        assert result == 0


# ---------------------------------------------------------------------------
# Per-page fetchers used by the coordinator schedules
# ---------------------------------------------------------------------------


class TestPerPageFetchers:
    """Tests for async_get_current_weather / async_get_hourly_forecast."""

    @pytest.fixture
    def api_client(self) -> IdokepApiClient:
        """Return an API client with a mocked session."""
        return IdokepApiClient(Mock(spec=aiohttp.ClientSession))

    @pytest.mark.asyncio
    async def test_current_weather_uses_current_page(
        self, api_client: IdokepApiClient
    ) -> None:
        """Current conditions come from /idojaras/."""
        html = '<div class="ik current-temperature">15℃</div>'
        with patch.object(
            api_client._http_client, "get_html", AsyncMock(return_value=html)
        ) as get_html:
            result = await api_client.async_get_current_weather("Budapest")

        get_html.assert_awaited_once_with("https://www.idokep.hu/idojaras/Budapest")
        assert result["temperature"] == 15

    @pytest.mark.asyncio
    async def test_hourly_forecast_downloads_page_once(
        self, api_client: IdokepApiClient
    ) -> None:
        """Hourly forecast and alerts are parsed from a single download."""
        html = """
        <div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">16:00</div>
            <div class="ik tempValue"><a>13</a></div>
        </div>
        """
        with patch.object(
            api_client._http_client, "get_html", AsyncMock(return_value=html)
        ) as get_html:
            result = await api_client.async_get_hourly_forecast("Budapest")

        get_html.assert_awaited_once_with("https://www.idokep.hu/elorejelzes/Budapest")
        assert len(result["hourly_forecast"]) == 1
        assert result["alerts"] == []
        assert result["alerts_by_level"] == {"yellow": [], "orange": [], "red": []}

    @pytest.mark.asyncio
    async def test_network_errors_propagate(self, api_client: IdokepApiClient) -> None:
        """Unlike async_get_weather_data, fetch errors are not swallowed."""
        with (
            patch.object(
                api_client._http_client,
                "get_html",
                AsyncMock(side_effect=IdokepApiClientCommunicationError("down")),
            ),
            pytest.raises(IdokepApiClientCommunicationError),
        ):
            await api_client.async_get_current_weather("Budapest")


# ---------------------------------------------------------------------------
# Daily column dates and today's max/min on the forecast page
# ---------------------------------------------------------------------------


def daily_col(title: str, high: int, low: int) -> str:
    """Return a daily forecast column as served by idokep.hu (2026-10)."""
    return f"""
    <div class="ik dailyForecastCol">
        <div class="ik dfIconAlert">
            <a class="ik d-block w-100 ik interact" title="{title}"></a>
        </div>
        <div class="ik min-max-container">
            <div class="ik max"><a>{high}</a></div>
            <div class="ik min"><a>{low}</a></div>
        </div>
    </div>
    """


def frozen_now(*args: int) -> type[dt.datetime]:
    """Return a datetime class whose now() is the given Budapest time."""
    fixed = dt.datetime(*args, tzinfo=zoneinfo.ZoneInfo("Europe/Budapest"))

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return fixed.astimezone(tz)

    return FrozenDateTime


THU = daily_col("Csütörtök<br>2026. október 29.", 16, 6)
FRI = daily_col("Péntek<br>2026. október 30.", 14, 7)


class TestDailyColumnDate:
    """DailyForecastParser.extract_date."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Csütörtök<br>2026. október 29.", dt.date(2026, 10, 29)),
            ("Kedd<br>2026. november 3.", dt.date(2026, 11, 3)),
            ("Szerda<br>2026. december 31.", dt.date(2026, 12, 31)),
            ("Csütörtök<br>2027. január 1.", dt.date(2027, 1, 1)),
            ("Hétfő<br>2026. május 4.", dt.date(2026, 5, 4)),
        ],
    )
    def test_date_from_title(self, title: str, expected: dt.date) -> None:
        """The column's icon link title holds its date."""
        col = BeautifulSoup(daily_col(title, 1, 0), "html.parser").find("div")
        assert DailyForecastParser().extract_date(col) == expected

    @pytest.mark.parametrize(
        "title", ["", "Csütörtök", "2026. foo 29.", "2026. február 30."]
    )
    def test_unparsable_title(self, title: str) -> None:
        """Missing, unknown month or impossible dates give None."""
        col = BeautifulSoup(daily_col(title, 1, 0), "html.parser").find("div")
        assert DailyForecastParser().extract_date(col) is None


class TestTodayMinMax:
    """DailyForecastParser.parse_today_min_max (forecast page)."""

    def parse_at(
        self, parser: DailyForecastParser, html: str, *now: int
    ) -> dict[str, int]:
        """Parse today's max/min from html as if it were the given time."""
        soup = BeautifulSoup(html, "html.parser")
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(*now),
        ):
            return parser.parse_today_min_max(soup)

    def test_uses_column_dated_today(self) -> None:
        """Daytime: today's column is first."""
        parser = DailyForecastParser()
        result = self.parse_at(parser, THU + FRI, 2026, 10, 29, 10, 0)
        assert result == {"temperature_max_today": 16, "temperature_min_today": 6}

    def test_never_takes_tomorrow_as_today(self) -> None:
        """Evening on a fresh start: the strip starts with tomorrow, no value."""
        parser = DailyForecastParser()
        assert self.parse_at(parser, FRI, 2026, 10, 29, 20, 51) == {}

    def test_keeps_todays_values_after_column_is_dropped(self) -> None:
        """Values seen earlier today stay once the page drops today's column."""
        parser = DailyForecastParser()
        self.parse_at(parser, THU + FRI, 2026, 10, 29, 10, 0)
        result = self.parse_at(parser, FRI, 2026, 10, 29, 20, 51)
        assert result == {"temperature_max_today": 16, "temperature_min_today": 6}

    def test_kept_values_expire_at_midnight(self) -> None:
        """Yesterday's values are not shown as today's."""
        parser = DailyForecastParser()
        self.parse_at(parser, THU + FRI, 2026, 10, 29, 10, 0)
        sat = daily_col("Szombat<br>2026. október 31.", 12, 7)
        # Just after midnight a cached page may still start with Friday...
        assert self.parse_at(parser, FRI + sat, 2026, 10, 30, 0, 5) == {
            "temperature_max_today": 14,
            "temperature_min_today": 7,
        }
        # ...and without any column for the 30th there is no value at all
        fresh = DailyForecastParser()
        self.parse_at(fresh, THU, 2026, 10, 29, 10, 0)
        assert self.parse_at(fresh, sat, 2026, 10, 30, 0, 5) == {}

    @pytest.mark.asyncio
    async def test_part_of_forecast_page_result(self) -> None:
        """The forecast page download includes today's max/min."""
        client = IdokepApiClient(Mock(spec=aiohttp.ClientSession))
        with (
            patch.object(
                client._http_client, "get_html", AsyncMock(return_value=THU + FRI)
            ),
            patch(
                "custom_components.idokep_weather_and_forecast.api.dt.datetime",
                frozen_now(2026, 10, 29, 10, 0),
            ),
        ):
            result = await client.async_get_hourly_forecast("Budapest")
        assert result["temperature_max_today"] == 16
        assert result["temperature_min_today"] == 6
        assert [d["datetime"] for d in result["daily_forecast"]] == [
            "2026-10-29",
            "2026-10-30",
        ]

    def test_restored_values_kept_until_midnight(self) -> None:
        """Values restored after a restart fill in once today's column is gone."""
        parser = DailyForecastParser()
        parser.restore_today_forecast(
            {"datetime": "2026-10-29", "temperature": 16, "templow": 6}
        )
        assert self.parse_at(parser, FRI, 2026, 10, 29, 21, 0) == {
            "temperature_max_today": 16,
            "temperature_min_today": 6,
        }
        assert self.parse_at(parser, FRI, 2026, 10, 30, 0, 5) == {
            "temperature_max_today": 14,
            "temperature_min_today": 7,
        }


class TestDailyForecastDates:
    """DailyForecastParser uses each column's own date."""

    def parse_at(self, html: str, *now: int) -> list[str]:
        """Return the forecast dates parsed from html at the given time."""
        soup = BeautifulSoup(html, "html.parser")
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(*now),
        ):
            result = DailyForecastParser().parse(soup)
        return [item["datetime"] for item in result.get("daily_forecast", [])]

    def test_daytime_starts_today(self) -> None:
        """During the day the strip starts with today."""
        assert self.parse_at(THU + FRI, 2026, 10, 29, 10, 0) == [
            "2026-10-29",
            "2026-10-30",
        ]

    def test_evening_strip_starting_tomorrow(self) -> None:
        """In the evening the first column is tomorrow and is labelled so."""
        sat = daily_col("Szombat<br>2026. október 31.", 12, 7)
        assert self.parse_at(FRI + sat, 2026, 10, 29, 20, 51) == [
            "2026-10-30",
            "2026-10-31",
        ]

    def test_values_stay_with_their_date(self) -> None:
        """Friday's high/low are reported for Friday, not for today."""
        soup = BeautifulSoup(FRI, "html.parser")
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 20, 51),
        ):
            item = DailyForecastParser().parse(soup)["daily_forecast"][0]
        assert item["datetime"] == "2026-10-30"
        assert (item["temperature"], item["templow"]) == (14, 7)

    def test_past_columns_dropped(self) -> None:
        """A page cached before midnight does not report yesterday."""
        assert self.parse_at(THU + FRI, 2026, 10, 30, 0, 2) == ["2026-10-30"]

    def test_unreadable_date_follows_previous_column(self) -> None:
        """A column without a date is taken as the day after the previous one."""
        undated = daily_col("", 10, 3)
        assert self.parse_at(FRI + undated, 2026, 10, 29, 20, 51) == [
            "2026-10-30",
            "2026-10-31",
        ]

    def test_no_dates_at_all_counts_from_today(self) -> None:
        """Without any readable date the old behaviour remains: today onwards."""
        html = daily_col("", 10, 3) + daily_col("", 11, 4)
        assert self.parse_at(html, 2026, 10, 29, 20, 51) == [
            "2026-10-29",
            "2026-10-30",
        ]

    def test_today_kept_until_midnight(self) -> None:
        """Once today's column is dropped, today's entry stays first until 0:00."""
        parser = DailyForecastParser()
        sat = daily_col("Szombat<br>2026. október 31.", 12, 7)

        def dates_at(html: str, *now: int) -> list[str]:
            with patch(
                "custom_components.idokep_weather_and_forecast.api.dt.datetime",
                frozen_now(*now),
            ):
                result = parser.parse(BeautifulSoup(html, "html.parser"))
            return [item["datetime"] for item in result["daily_forecast"]]

        assert dates_at(THU + FRI, 2026, 10, 29, 10, 0) == ["2026-10-29", "2026-10-30"]
        # Evening: the page starts with Friday, today's entry is put back
        assert dates_at(FRI + sat, 2026, 10, 29, 21, 0) == [
            "2026-10-29",
            "2026-10-30",
            "2026-10-31",
        ]
        # After midnight Thursday is gone for good
        assert dates_at(FRI + sat, 2026, 10, 30, 0, 0) == ["2026-10-30", "2026-10-31"]

    def test_kept_today_entry_is_the_full_forecast(self) -> None:
        """The kept entry has today's temperatures, condition etc."""
        parser = DailyForecastParser()
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 10, 0),
        ):
            first = parser.parse(BeautifulSoup(THU, "html.parser"))["daily_forecast"][0]
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 21, 0),
        ):
            kept = parser.parse(BeautifulSoup(FRI, "html.parser"))["daily_forecast"][0]
        assert kept == first
        assert (kept["temperature"], kept["templow"]) == (16, 6)

    def test_fresh_start_in_the_evening_starts_tomorrow(self) -> None:
        """Without having seen today's column there is nothing to put back."""
        assert self.parse_at(FRI, 2026, 10, 29, 21, 0) == ["2026-10-30"]

    def test_restored_today_entry_is_put_back(self) -> None:
        """Today's entry restored after a restart is shown until midnight."""
        parser = DailyForecastParser()
        parser.restore_today_forecast({"datetime": "2026-10-29", "temperature": 16})
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 21, 0),
        ):
            result = parser.parse(BeautifulSoup(FRI, "html.parser"))
        assert [d["datetime"] for d in result["daily_forecast"]] == [
            "2026-10-29",
            "2026-10-30",
        ]

    def test_year_change(self) -> None:
        """Dates run correctly from December into January."""
        dec = daily_col("Csütörtök<br>2026. december 31.", 2, -3)
        jan = daily_col("Péntek<br>2027. január 1.", 1, -4)
        assert self.parse_at(dec + jan, 2026, 12, 31, 12, 0) == [
            "2026-12-31",
            "2027-01-01",
        ]


class TestWindStrength:
    """WindStrengthMapper and HourlyForecastParser.extract_wind_strength."""

    @pytest.mark.parametrize(
        ("css_class", "level"),
        [
            ("gyenge", "gentle_breeze"),
            ("mersekelt", "moderate_breeze"),
            ("elenk", "fresh_breeze"),
            ("eros", "strong_breeze"),
            ("viharos", "gale"),
            ("orkan", "hurricane"),
            ("ismeretlen", None),
        ],
    )
    def test_from_class(self, css_class: str, level: str | None) -> None:
        """CSS classes as on the site (accent-free words)."""
        assert WindStrengthMapper.from_class(css_class) == level

    @pytest.mark.parametrize(
        ("tooltip", "level"),
        [
            ("Mérsékelt északi szél", "moderate_breeze"),
            ("Élénk északnyugati szél", "fresh_breeze"),
            ("Gyenge déli szél", "gentle_breeze"),
            ("Gyenge szellő", "light_air"),
            ("Erős keleti szél", "strong_breeze"),
            ("Erős vihar", "strong_gale"),
            ("Igen erős szél", "near_gale"),
            ("Szélcsend", "calm"),
            # Official Hungarian Beaufort names
            ("Alig érezhető szél", "light_air"),
            ("Könnyű szél", "light_breeze"),
            ("Heves vihar", "strong_gale"),
            ("Dühöngő vihar", "storm"),
            ("Heves szélvihar", "violent_storm"),
            ("Orkán", "hurricane"),
            ("Valami más", None),
        ],
    )
    def test_from_text(self, tooltip: str, level: str | None) -> None:
        """Tooltips: the longest matching word wins ("Erős vihar" vs "Erős")."""
        assert WindStrengthMapper.from_text(tooltip) == level

    def test_speeds_follow_beaufort_midpoints(self) -> None:
        """Estimates: middle of each Beaufort range in km/h."""
        speeds = WindStrengthMapper.SPEEDS
        assert list(speeds.values()) == [
            0,
            3,
            9,
            16,
            24,
            34,
            44,
            56,
            68,
            82,
            96,
            110,
            118,
        ]
        assert WindStrengthMapper.levels() == list(speeds)

    def _card(self, wind_class: str, tooltip: str) -> Tag:
        html = f"""
        <div class="ik wide-hourly-forecast-card">
            <div class="ik hourly-wind">
                <a class="ik hover-over d-block" data-bs-content="{tooltip}">
                    <div class="ik wind {wind_class}" style="--rotateAngle:6deg"></div>
                </a>
            </div>
        </div>
        """
        return BeautifulSoup(html, "html.parser").find("div")

    def test_card_class(self) -> None:
        """The class is used when known."""
        card = self._card("elenk", "Élénk északi szél")
        assert HourlyForecastParser().extract_wind_strength(card) == "fresh_breeze"

    def test_card_unknown_class_falls_back_to_tooltip(self) -> None:
        """An unknown class (e.g. a new site word) falls back to the tooltip."""
        card = self._card("xyz", "Viharos északi szél")
        assert HourlyForecastParser().extract_wind_strength(card) == "gale"

    def test_card_unknown_everything(self) -> None:
        """Nothing recognisable: no strength, so no estimate."""
        card = self._card("xyz", "Ismeretlen")
        assert HourlyForecastParser().extract_wind_strength(card) is None
        no_wind = BeautifulSoup("<div></div>", "html.parser").find("div")
        assert HourlyForecastParser().extract_wind_strength(no_wind) is None
