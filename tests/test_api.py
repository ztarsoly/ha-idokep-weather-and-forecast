"""Unit tests for the Időkép API client."""

# Tests reach into the integration's private helpers
# pylint: disable=protected-access, duplicate-code

from __future__ import annotations

import datetime as dt
import zoneinfo
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest

from custom_components.idokep_weather_and_forecast.api import (
    _BROWSER_HEADERS,
    AlertParser,
    CurrentWeatherParser,
    DailyForecastParser,
    HourlyForecastParser,
    HttpClient,
    IdokepApiClient,
    IdokepApiClientCommunicationError,
    IdokepApiClientError,
    IdokepApiClientUnknownLocationError,
    TimeUtils,
    WeatherConditionMapper,
    WindStrengthMapper,
    station_map_average,
)
from custom_components.idokep_weather_and_forecast.failures import FailureRecords
from custom_components.idokep_weather_and_forecast.html_tree import Element, parse_html


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


class TestConnectionCheck:
    """The check before a setup that would have nothing to show."""

    CURRENT = "https://www.idokep.hu/idojaras/Eger"
    FORECAST = "https://www.idokep.hu/elorejelzes/Eger"

    @staticmethod
    def _session(*answers: object) -> tuple[Mock, Mock]:
        """Return (session, response); answers: None served, else raised."""
        response = Mock()
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        session = Mock(spec=aiohttp.ClientSession)
        session.get = Mock(side_effect=[answer or response for answer in answers])
        return session, response

    async def test_current_page_served(self) -> None:
        """One request is enough, and the page is not read."""
        session, response = self._session(None)

        await IdokepApiClient(session).async_check_connection("Eger")

        session.get.assert_called_once_with(
            self.CURRENT, headers=_BROWSER_HEADERS
        )
        response.raise_for_status.assert_called_once()
        response.text.assert_not_called()

    async def test_forecast_page_served(self) -> None:
        """The forecast page is tried if the current one is not served."""
        session, _ = self._session(aiohttp.ClientError("down"), None)

        await IdokepApiClient(session).async_check_connection("Eger")

        assert [c.args[0] for c in session.get.call_args_list] == [
            self.CURRENT,
            self.FORECAST,
        ]

    async def test_neither_page_served(self) -> None:
        """The last failure is raised."""
        session, _ = self._session(TimeoutError(), TimeoutError())

        error = IdokepApiClientCommunicationError
        with pytest.raises(error, match=self.FORECAST):
            await IdokepApiClient(session).async_check_connection("Eger")


def current_page(title: str, temperature: str = "8 \u2103") -> str:
    """Return a current conditions page with a title and a temperature."""
    return (
        f"<html><head><title>{title}</title></head><body>"
        f'<div class="current-temperature">{temperature}</div></body></html>'
    )


def forecast_page(served: str) -> str:
    """Return a forecast page with a settlement's title."""
    return (
        f"<html><head><title>{served} időjárás előrejelzés | Időkép</title></head>"
        "<body></body></html>"
    )


class TestLocationCheck:
    """The setup forms' check that idokep.hu knows the settlement."""

    CURRENT = "https://www.idokep.hu/idojaras/Nincsilyen"
    FORECAST = "https://www.idokep.hu/elorejelzes/Nincsilyen"
    DOWN = IdokepApiClientCommunicationError("current page: 500")

    @staticmethod
    async def _fails(current: object, forecast: object) -> tuple[Exception, list]:
        """Run the check on these two answers; return the error and the URLs."""
        client = IdokepApiClient(Mock(spec=aiohttp.ClientSession))
        with patch.object(
            HttpClient, "get_html", AsyncMock(side_effect=[current, forecast])
        ) as download:
            with pytest.raises(IdokepApiClientError) as raised:
                await client.async_validate_location("Nincsilyen")
        return raised.value, [c.args[0] for c in download.await_args_list]

    @pytest.mark.parametrize(
        "current",
        [DOWN, current_page("Karbantartás")],
        ids=["not-served", "no-weather-data"],
    )
    async def test_the_forecast_page_shows_an_unknown_name(
        self, current: object
    ) -> None:
        """Budapest's forecast for another name: unknown, whatever failed."""
        error, urls = await self._fails(current, forecast_page("Budapest"))

        assert isinstance(error, IdokepApiClientUnknownLocationError)
        assert str(error) == "idokep.hu served Budapest for Nincsilyen"
        assert urls == [self.CURRENT, self.FORECAST]

    @pytest.mark.parametrize(
        "forecast",
        [
            forecast_page("Nincsilyen"),
            IdokepApiClientCommunicationError("forecast page: 500"),
            current_page("Karbantartás"),
        ],
        ids=["its-own", "not-served", "no-settlement"],
    )
    async def test_the_current_pages_error_stays_otherwise(
        self, forecast: object
    ) -> None:
        """A forecast page can't confirm a name: the check has still failed."""
        error, urls = await self._fails(self.DOWN, forecast)

        assert error is self.DOWN
        assert urls == [self.CURRENT, self.FORECAST]

    @staticmethod
    async def _validate(location: str, page: str) -> tuple[str, AsyncMock]:
        """Run the check on a served page; return the name and the download."""
        client = IdokepApiClient(Mock(spec=aiohttp.ClientSession))
        with patch.object(
            HttpClient, "get_html", AsyncMock(return_value=page)
        ) as download:
            name = await client.async_validate_location(location)
        return name, download

    @pytest.mark.parametrize(
        ("location", "served", "spelled"),
        [
            ("Debrecen-Józsa", "Debrecen-Józsa", "Debrecen-Józsa"),
            ("debrecen-jozsa", "Debrecen-Józsa", "Debrecen-Józsa"),
            ("GYŐR", "Győr", "Győr"),
            ("budapest xi. ker", "Budapest  XI.\n ker", "Budapest XI. ker"),
        ],
    )
    async def test_the_settlements_page_is_accepted(
        self, location: str, served: str, spelled: str
    ) -> None:
        """Letter case and accents don't matter; the page's spelling is returned."""
        name, download = await self._validate(
            location, current_page(f"{served} időjárás - Időkép")
        )

        assert name == spelled
        download.assert_awaited_once_with(
            f"https://www.idokep.hu/idojaras/{location}"
        )

    @pytest.mark.parametrize(
        "location", ["Nincsilyen", "gyor", "Debrecen józsa", "", "Budapest/x"]
    )
    async def test_budapests_page_for_another_name_is_refused(
        self, location: str
    ) -> None:
        """The site answers a name it doesn't know with Budapest's page."""
        page = current_page("Budapest időjárás - Időkép")

        error = IdokepApiClientUnknownLocationError
        with pytest.raises(error, match="served Budapest"):
            await self._validate(location, page)

    @pytest.mark.parametrize(
        "page",
        [
            current_page("Eger időjárás - Időkép", temperature=""),
            current_page("Karbantartás"),
            "<html><body></body></html>",
        ],
        ids=["no-temperature", "other-title", "no-title"],
    )
    async def test_a_page_without_weather_data_is_a_failed_download(
        self, page: str
    ) -> None:
        """Such a page says nothing about the settlement."""
        error = IdokepApiClientCommunicationError
        with pytest.raises(error, match="No weather data"):
            await self._validate("Eger", page)


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
        assert WeatherConditionMapper.map_condition("Intenzív eső") == "pouring"
        assert WeatherConditionMapper.map_condition("Hózivatar") == "snowy"
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

    def test_unknown_description_is_warned_about_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each unrecognised description is warned about only once."""
        records = FailureRecords()

        for text in ("Furcsa idő", "Furcsa idő", "napos", "más"):
            WeatherConditionMapper.map_condition(text, records)

        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", "Unknown idokep.hu weather 'Furcsa idő'"),
            ("WARNING", "Unknown idokep.hu weather 'más'"),
        ]

    def test_the_clients_parsers_share_the_records(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warned once for an entry's setup, whichever page says it."""
        records = FailureRecords()
        client = IdokepApiClient(Mock(spec=aiohttp.ClientSession), records)
        page = parse_html('<div class="ik current-weather">Furcsa idő</div>')

        client._current_parser.parse(page)
        client._current_parser.parse(page)

        assert [r.getMessage() for r in caplog.records] == [
            "Unknown idokep.hu weather 'Furcsa idő'"
        ]
        for parser in (
            client._current_parser,
            client._hourly_parser,
            client._daily_parser,
            client._alert_parser,
        ):
            assert parser._records is records


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
            "custom_components.idokep_weather_and_forecast.api.asyncio.timeout"
        ):
            result = await api_client.async_get_current_weather("Budapest")

        assert set(result) == {"temperature", "condition", "cloud_coverage", "storm_now"}
        assert result["temperature"] == 22
        assert result["condition"] == "sunny"
        assert result["cloud_coverage"] == 0

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
            "custom_components.idokep_weather_and_forecast.api.asyncio.timeout"
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
        # Hourly amounts are numbers (the hidden mm value, see its tests)
        for item in forecast:
            assert isinstance(item["precipitation"], (int, float))

        # Verify last hour of Day 1 (23:00)
        day1_last = forecast[23]
        assert day1_last["temperature"] == 10 - 23
        assert day1_last["condition"] == "cloudy"
        assert day1_last["precipitation_probability"] == 10 + (23 * 2)

        # Verify first hour of Day 2 (00:00, item 24)
        day2_first = forecast[24]
        assert day2_first["temperature"] == -5
        assert day2_first["condition"] == "snowy"
        assert day2_first["precipitation_probability"] == 50

        # Verify last hour (Day 2, 11:00)
        last_item = forecast[35]
        assert last_item["temperature"] == -5 - 11
        assert last_item["condition"] == "snowy"
        assert last_item["precipitation_probability"] == 50 + (11 * 3)

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
            "custom_components.idokep_weather_and_forecast.api.asyncio.timeout"
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
            "custom_components.idokep_weather_and_forecast.api.asyncio.timeout"
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

    def test_extract_daily_temperature(self, api_client: IdokepApiClient) -> None:
        """Test daily temperature extraction."""
        html = """
        <div class="col">
            <div class="ik min"><a>-5</a></div>
            <div class="ik max"><a>15</a></div>
        </div>
        """
        soup = parse_html(html)
        col = soup.find("div", class_="col")

        if isinstance(col, Element):
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
        soup = parse_html(html)
        col = soup.find("div", class_="col")

        if isinstance(col, Element):
            condition = api_client._daily_parser.extract_condition(col)
            assert condition == "rainy"
            item = api_client._daily_parser._parse_daily_column(col, dt.date(2026, 11, 17))
            assert item["condition"] == "rainy"
            assert item["cloud_coverage"] == 100

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
        soup = parse_html(html)
        col = soup.find("div", class_="col")

        if isinstance(col, Element):
            condition = api_client._daily_parser.extract_condition(col)
            assert condition == "sunny"


# ---------------------------------------------------------------------------
# AlertParser tests
# ---------------------------------------------------------------------------


class TestAlertParserWarning:
    """Tests for AlertParser._parse_warning_level(): the banner."""

    # As on the forecast pages of November 2026
    BOX = """
        <div class="container position-relative siteMainOuter"><a>
        <div class="background selectiveYellow color-darkblue p-2 mt-2 mb-2 rounded inlineWeatherAlert">
            <i class="fas fa-fw fa-exclamation-triangle"></i> {text}
        </div></a></div>
    """

    @pytest.fixture
    def parser(self) -> AlertParser:
        """Return a fresh AlertParser instance."""
        return AlertParser()

    def test_no_banner_is_no_warning(self, parser: AlertParser) -> None:
        """Without a banner the warning level is none."""
        result = parser.parse(parse_html("<div>no alerts here</div>"))
        assert result["warning_level"] == "none"
        assert result["alerts"] == []

    @pytest.mark.parametrize(
        ("text", "level"),
        [
            ("Sárga riasztás viharos szélre", "yellow"),
            ("Sárga figyelmeztetés zivatarra", "yellow"),
            ("Sárga figyelmeztetés viharos szélre, kiadós esőzésre", "yellow"),
            ("Narancs riasztás szél miatt", "orange"),
            ("Piros figyelmeztetés hó miatt", "red"),
            ("Vörös riasztás hőség miatt", "red"),
            ("Figyelmeztetés", "none"),  # no level named
        ],
    )
    def test_banner_level(self, parser: AlertParser, text: str, level: str) -> None:
        """The level comes from the banner's text, an alert's or a warning's."""
        result = parser.parse(parse_html(self.BOX.format(text=text)))
        assert result["warning_level"] == level

    def test_banner_is_not_an_alert(self, parser: AlertParser) -> None:
        """Only the hourly cards' icons are alerts, even when the banner is one."""
        html = self.BOX.format(text="Sárga riasztás viharos szélre") + "".join(
            '<div class="ik genericHourlyAlert"></div>' for _ in range(36)
        )
        result = parser.parse(parse_html(html))
        assert result["alerts"] == []
        assert result["warning_level"] == "yellow"

    def test_old_top_alert_bar_is_not_read(self, parser: AlertParser) -> None:
        """idokep.hu no longer has #topalertbar; it isn't used."""
        html = """
        <div id="topalertbar" class="yellow"><a>Sárga riasztás szél miatt</a></div>
        """
        assert parser.parse(parse_html(html))["warning_level"] == "none"

    def test_empty_page_has_no_alerts_or_warnings(self, parser: AlertParser) -> None:
        """Without alerts both lists are empty."""
        result = parser.parse(parse_html(""))
        assert result == {"alerts": [], "warnings": [], "warning_level": "none"}

    @pytest.mark.parametrize(
        ("text", "warnings"),
        [
            ("Sárga riasztás viharos szélre", [{"type": "wind", "level": "yellow"}]),
            ("Sárga figyelmeztetés zivatarra", [{"type": "thunderstorm", "level": "yellow"}]),
            ("Piros figyelmeztetés hó miatt", [{"type": "snow", "level": "red"}]),
            (
                # The page on 2026-11-19: one banner, two types
                "Sárga figyelmeztetés zivatarra, viharos szélre",
                [
                    {"type": "thunderstorm", "level": "yellow"},
                    {"type": "wind", "level": "yellow"},
                ],
            ),
            ("Figyelmeztetés", []),
        ],
    )
    def test_banner_warnings(
        self, parser: AlertParser, text: str, warnings: list[dict[str, str]]
    ) -> None:
        """A banner's type and level are a warning."""
        result = parser.parse(parse_html(self.BOX.format(text=text)))
        assert result["warnings"] == warnings

    def test_several_banners(self, parser: AlertParser) -> None:
        """Every banner is a warning; the highest level is the sensor's."""
        html = (
            self.BOX.format(text="Sárga figyelmeztetés ónos esőre")
            + self.BOX.format(text="Narancs figyelmeztetés hófúvásra")
            + self.BOX.format(text="Sárga figyelmeztetés hófúvásra")
        )
        result = parser.parse(parse_html(html))
        assert result["warnings"] == [
            {"type": "blowing_snow", "level": "orange"},
            {"type": "freezing_rain", "level": "yellow"},
        ]
        assert result["warning_level"] == "orange"


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
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0] == {"type": "wind", "level": "yellow"}

    def test_hourly_alert_orange_narancs(self, parser: AlertParser) -> None:
        """'Narancs' in description → orange level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Narancs riasztás eső miatt">
                <img class="forecast-alert-icon" src="/icons/rain.png" />
            </a>
        </div>
        """
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert len(result) == 1
        assert result[0] == {"type": "rain", "level": "orange"}

    def test_hourly_alert_red_piros(self, parser: AlertParser) -> None:
        """'Piros' in description → red level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Piros riasztás hóvihar miatt">
                <img class="forecast-alert-icon" src="/icons/snow.png" />
            </a>
        </div>
        """
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert result == [{"type": "blowing_snow", "level": "red"}]

    def test_hourly_alert_red_voros(self, parser: AlertParser) -> None:
        """'Vörös' in description → red level."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Vörös riasztás vihar miatt">
                <img class="forecast-alert-icon" src="https://cdn.example.com/alert.png" />
            </a>
        </div>
        """
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert result == [{"type": "wind", "level": "red"}]

    def test_hourly_alert_no_recognised_level(self, parser: AlertParser) -> None:
        """Description without a recognised level keyword → no alert."""
        html = """
        <div class="genericHourlyAlert">
            <a class="hover-over" data-bs-content="Unknown alert text">
                <img class="forecast-alert-icon" src="/icons/alert.png" />
            </a>
        </div>
        """
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_no_hover_over_link(self, parser: AlertParser) -> None:
        """Container without a hover-over link → no alert."""
        html = """
        <div class="genericHourlyAlert">
            <span>Sárga riasztás</span>
        </div>
        """
        soup = parse_html(html)
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
        soup = parse_html(html)
        result = parser._parse_hourly_alerts(soup)
        assert result == []

    def test_hourly_alert_no_containers(self, parser: AlertParser) -> None:
        """No genericHourlyAlert divs → empty list."""
        soup = parse_html("<div>nothing</div>")
        result = parser._parse_hourly_alerts(soup)
        assert result == []

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
        soup = parse_html(html)
        assert parser.parse(soup)["alerts"] == [{"type": "wind", "level": "yellow"}]


class TestAlertParserHelpers:
    """Tests for _known_type, _alerts_from_text and _merge."""

    @pytest.mark.parametrize(
        ("description", "expected_type"),
        [
            ("Sárga riasztás ónos esőre", "freezing_rain"),
            ("ónosesőre vonatkozó riasztás", "freezing_rain"),
            ("Sárga riasztás hófúvásra", "blowing_snow"),
            ("Piros riasztás hóvihar miatt", "blowing_snow"),
            ("Narancs riasztás zivatar", "thunderstorm"),
            ("Sárga riasztás viharos szélre", "wind"),
            ("vihar miatt", "wind"),
            ("Sárga riasztás szélre", "wind"),
            ("Sárga riasztás esőre", "rain"),
            ("Sárga figyelmeztetés kiadós esőzésre", "rain"),
            ("Narancs riasztás havazásra", "snow"),
            ("Piros figyelmeztetés hó miatt", "snow"),
            ("Sárga riasztás ködre", "fog"),
            ("Sárga riasztás fagyra", "frost"),
            ("Sárga riasztás extrém hidegre", "frost"),
            ("Narancs riasztás hőségre", "heat"),
            ("Sárga figyelmeztetés melegre", "heat"),
        ],
    )
    def test_known_type(self, description: str, expected_type: str) -> None:
        """The text's hazard word gives idokep.hu's own type name."""
        assert AlertParser._known_type(description) == expected_type

    def test_unknown_type_is_other(self) -> None:
        """A text with a level but no known hazard word is "other"."""
        assert AlertParser._known_type("Sárga riasztás") is None
        assert AlertParser._alerts_from_text("Sárga riasztás") == [
            {"type": "other", "level": "yellow"}
        ]

    def test_no_level_no_alert(self) -> None:
        """A text without a level gives nothing."""
        # An empty list or dict, not just a falsy value
        # pylint: disable-next=use-implicit-booleaness-not-comparison
        assert AlertParser._alerts_from_text("zivatar várható") == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Sárga figyelmeztetés zivatarra, viharos szélre",
                [
                    {"type": "thunderstorm", "level": "yellow"},
                    {"type": "wind", "level": "yellow"},
                ],
            ),
            (
                "Sárga riasztás zivatarra és viharos szélre",
                [
                    {"type": "thunderstorm", "level": "yellow"},
                    {"type": "wind", "level": "yellow"},
                ],
            ),
            (
                "Sárga figyelmeztetés zivatarra, narancs riasztás viharos szélre",
                [
                    {"type": "thunderstorm", "level": "yellow"},
                    {"type": "wind", "level": "orange"},
                ],
            ),
        ],
    )
    def test_several_types_in_one_text(self, text: str, expected: list) -> None:
        """Each named type is an item; a part without a level has the one before."""
        assert AlertParser._alerts_from_text(text) == expected

    def test_merge_keeps_each_types_highest_level_highest_first(self) -> None:
        """One item per type at its highest level; highest first, then type order."""
        items = [
            {"type": "other", "level": "yellow"},
            {"type": "fog", "level": "yellow"},
            {"type": "heat", "level": "orange"},
            {"type": "thunderstorm", "level": "yellow"},
            {"type": "rain", "level": "yellow"},
            {"type": "rain", "level": "orange"},
            {"type": "wind", "level": "red"},
        ]
        assert AlertParser._merge(items) == [
            {"type": "wind", "level": "red"},
            {"type": "rain", "level": "orange"},
            {"type": "heat", "level": "orange"},
            {"type": "thunderstorm", "level": "yellow"},
            {"type": "fog", "level": "yellow"},
            {"type": "other", "level": "yellow"},
        ]


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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        result = parser.parse(parse_html(html))
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
        {rain}
    </div>
    """
    RAIN_HTML = """
        <div class="ik hourly-rain-chance">
            <i class="fas fa-umbrella"></i>
            <a class="ik interact" data-bs-content="Csapadék valószínűsége: {p}%">{p}%</a>
        </div>
        <div class="ik hourly-rainlevel-container"><div class="ik hourly-rainlevel">
            <!--{p_mm} mm--></div>
            <div class="ik hourly-rainlevel-meter" style="height: 12px;"></div>
        </div>
    """

    def test_extract_wind_bearing(self, parser: HourlyForecastParser) -> None:
        """Bearing is read from the arrow's --rotateAngle style."""
        html = self.WIND_CARD_HTML.format(rain="", hour="16:00", angle=6)
        card = parse_html(html).find("div")
        assert parser.extract_wind_bearing(card) == 6

    def test_extract_wind_bearing_normalises_to_0_359(
        self, parser: HourlyForecastParser
    ) -> None:
        """Angles outside 0-359 are wrapped."""
        html = self.WIND_CARD_HTML.format(rain="", hour="16:00", angle=370)
        card = parse_html(html).find("div")
        assert parser.extract_wind_bearing(card) == 10

    def test_extract_wind_bearing_missing(self, parser: HourlyForecastParser) -> None:
        """No wind element or no angle gives None."""
        no_wind = parse_html("<div class='card'></div>").find("div")
        assert parser.extract_wind_bearing(no_wind) is None

        no_angle = parse_html(
            "<div><div class='ik hourly-wind'><div class='ik wind gyenge'></div>"
            "</div></div>").find("div")
        assert parser.extract_wind_bearing(no_angle) is None

    def test_parse_adds_wind_bearing_to_items_and_current_hour(
        self, parser: HourlyForecastParser
    ) -> None:
        """Each hourly item gets wind_bearing; the current slot sets the top level."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        fixed_now = dt.datetime(2026, 10, 29, 16, 20, tzinfo=local_tz)

        class FrozenDateTime(dt.datetime):

            """The real datetime class, stopped at a fixed time."""

            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return fixed_now.astimezone(tz)

        html = self.WIND_CARD_HTML.format(
            rain="",
            hour="16:00",
            angle=6,
        ) + self.WIND_CARD_HTML.format(rain="", hour="17:00", angle=200)
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            result = parser.parse(parse_html(html))

        bearings = [item["wind_bearing"] for item in result["hourly_forecast"]]
        # Both cards say "mersekelt": moderate breeze, 20 km/h, gust 35
        assert result["wind_strength"] == "moderate_breeze"
        assert result["wind_speed"] == 20
        assert result["wind_gust_speed"] == 35
        for item in result["hourly_forecast"]:
            assert item["wind_strength"] == "moderate_breeze"
            assert item["native_wind_speed"] == 20
            assert item["native_wind_gust_speed"] == 35
        assert bearings == [6, 200]
        assert result["wind_bearing"] == 6

    def test_parse_current_hour_precipitation_probability(
        self, parser: HourlyForecastParser
    ) -> None:
        """The current hour's rain chance is the top-level value; none shown = 0."""
        local_tz = zoneinfo.ZoneInfo("Europe/Budapest")
        fixed_now = dt.datetime(2026, 10, 31, 21, 10, tzinfo=local_tz)

        class FrozenDateTime(dt.datetime):

            """The real datetime class, stopped at a fixed time."""

            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return fixed_now.astimezone(tz)

        html = self.WIND_CARD_HTML.format(
            rain=self.RAIN_HTML.format(p=18, p_mm=2), hour="21:00", angle=0
        ) + self.WIND_CARD_HTML.format(rain="", hour="22:00", angle=0)
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            result = parser.parse(parse_html(html))

        assert result["precipitation_probability"] == 18
        # The current hour's hidden amount (the Precipitation sensor)
        assert result["precipitation"] == 0.2
        assert [i["precipitation"] for i in result["hourly_forecast"]] == [0.2, 0]
        probabilities = [
            item["precipitation_probability"] for item in result["hourly_forecast"]
        ]
        assert probabilities == [18, 0]

    def test_no_current_hour_without_its_slot(self) -> None:
        """If no slot contains now, no other hour stands in for it."""
        forecast = [
            {"datetime": "2026-10-29T17:00:00+01:00", "wind_bearing": 90},
            {"datetime": "2026-10-29T18:00:00+01:00", "wind_bearing": 180},
        ]
        now = dt.datetime(
            2026, 10, 29, 16, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))
        )
        assert HourlyForecastParser._current_hour_item(forecast, now) is None

    def _parse_at(
        self, parser: HourlyForecastParser, now: dt.datetime, hours: list[str]
    ) -> dict:
        """Parse wind cards for the given hours with the clock frozen at now."""

        class FrozenDateTime(dt.datetime):

            """The real datetime class, stopped at a fixed time."""

            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return now.astimezone(tz)

        html = "".join(
            self.WIND_CARD_HTML.format(rain="", hour=hour, angle=90 * index)
            for index, hour in enumerate(hours)
        )
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            return parser.parse(parse_html(html))

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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
        card = soup.find("div", class_="ik wide-hourly-forecast-card")
        assert parser.extract_precipitation_probability(card) == 0

    # ---- extract_precipitation_amount -------------------------------------

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

            """The real datetime class, stopped at a fixed time."""

            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return fixed_utc.astimezone(tz)

        html = '<div class="ik dailyForecastCol"></div>'
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            FrozenDateTime,
        ):
            result = parser.parse(parse_html(html))
        assert result["daily_forecast"][0]["datetime"] == "2025-06-02T00:00:00+02:00"

    # ---- extract_temperature -----------------------------------------------

    def test_extract_temperature_no_match_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """Return None when the <a> tag contains no numeric text."""
        # Build a parent col div so find works correctly
        container = parse_html(
            '<div class="col"><div class="ik max"><a>no numbers</a></div></div>'
        ).find("div", class_="col")
        result = parser.extract_temperature(container, "ik max")
        assert result is None

    # ---- extract_condition -------------------------------------------------

    def test_extract_condition_no_df_icon_alert_returns_none(
        self, parser: DailyForecastParser
    ) -> None:
        """No dfIconAlert div → returns None."""
        html = "<div class='col'></div>"
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
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
        soup = parse_html(html)
        col = soup.find("div", class_="col")
        assert parser.extract_condition(col) is None

    # ---- extract_precipitation_probability (data-attribute path) -----------

    def test_extract_precipitation_no_span_returns_zero(
        self, parser: DailyForecastParser
    ) -> None:
        """No ik mm span → extract_precipitation returns 0."""
        html = "<div class='col'></div>"
        soup = parse_html(html)
        col = soup.find("div")
        assert parser.extract_precipitation(col) == 0

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
        assert result["warnings"] == []

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

        """The real datetime class, stopped at a fixed time."""

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
        col = parse_html(daily_col(title, 1, 0)).find("div")
        assert DailyForecastParser().extract_date(col) == expected

    @pytest.mark.parametrize(
        "title", ["", "Csütörtök", "2026. foo 29.", "2026. február 30."]
    )
    def test_unparsable_title(self, title: str) -> None:
        """Missing, unknown month or impossible dates give None."""
        col = parse_html(daily_col(title, 1, 0)).find("div")
        assert DailyForecastParser().extract_date(col) is None


class TestTodayMinMax:
    """DailyForecastParser.parse_today_min_max (forecast page)."""

    def parse_at(
        self, parser: DailyForecastParser, html: str, *now: int
    ) -> dict[str, int]:
        """Parse today's max/min from html as if it were the given time."""
        soup = parse_html(html)
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
        # An empty list or dict, not just a falsy value
        # pylint: disable-next=use-implicit-booleaness-not-comparison
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
        # An empty list or dict, not just a falsy value
        # pylint: disable-next=use-implicit-booleaness-not-comparison
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
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
        ]

    def test_restored_values_kept_until_midnight(self) -> None:
        """Values restored after a restart fill in once today's column is gone."""
        parser = DailyForecastParser()
        parser.restore_today_forecast(
            {"datetime": "2026-10-29T00:00:00+01:00", "temperature": 16, "templow": 6}
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
        soup = parse_html(html)
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(*now),
        ):
            result = DailyForecastParser().parse(soup)
        return [item["datetime"] for item in result.get("daily_forecast", [])]

    def test_daytime_starts_today(self) -> None:
        """During the day the strip starts with today."""
        assert self.parse_at(THU + FRI, 2026, 10, 29, 10, 0) == [
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
        ]

    def test_evening_strip_starting_tomorrow(self) -> None:
        """In the evening the first column is tomorrow and is labelled so."""
        sat = daily_col("Szombat<br>2026. október 31.", 12, 7)
        assert self.parse_at(FRI + sat, 2026, 10, 29, 20, 51) == [
            "2026-10-30T00:00:00+01:00",
            "2026-10-31T00:00:00+01:00",
        ]

    def test_values_stay_with_their_date(self) -> None:
        """Friday's high/low are reported for Friday, not for today."""
        soup = parse_html(FRI)
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 20, 51),
        ):
            item = DailyForecastParser().parse(soup)["daily_forecast"][0]
        assert item["datetime"] == "2026-10-30T00:00:00+01:00"
        assert (item["temperature"], item["templow"]) == (14, 7)

    def test_past_columns_dropped(self) -> None:
        """A page cached before midnight does not report yesterday."""
        assert self.parse_at(THU + FRI, 2026, 10, 30, 0, 2) == ["2026-10-30T00:00:00+01:00"]

    def test_unreadable_date_follows_previous_column(self) -> None:
        """A column without a date is taken as the day after the previous one."""
        undated = daily_col("", 10, 3)
        assert self.parse_at(FRI + undated, 2026, 10, 29, 20, 51) == [
            "2026-10-30T00:00:00+01:00",
            "2026-10-31T00:00:00+01:00",
        ]

    def test_no_dates_at_all_counts_from_today(self) -> None:
        """Without any readable date the old behaviour remains: today onwards."""
        html = daily_col("", 10, 3) + daily_col("", 11, 4)
        assert self.parse_at(html, 2026, 10, 29, 20, 51) == [
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
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
                result = parser.parse(parse_html(html))
            return [item["datetime"] for item in result["daily_forecast"]]

        assert dates_at(THU + FRI, 2026, 10, 29, 10, 0) == [
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
        ]
        # Evening: the page starts with Friday, today's entry is put back
        assert dates_at(FRI + sat, 2026, 10, 29, 21, 0) == [
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
            "2026-10-31T00:00:00+01:00",
        ]
        # After midnight Thursday is gone for good
        assert dates_at(FRI + sat, 2026, 10, 30, 0, 0) == [
            "2026-10-30T00:00:00+01:00",
            "2026-10-31T00:00:00+01:00",
        ]

    def test_kept_today_entry_is_the_full_forecast(self) -> None:
        """The kept entry has today's temperatures, condition etc."""
        parser = DailyForecastParser()
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 10, 0),
        ):
            first = parser.parse(parse_html(THU))["daily_forecast"][0]
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 21, 0),
        ):
            kept = parser.parse(parse_html(FRI))["daily_forecast"][0]
        assert kept == first
        assert (kept["temperature"], kept["templow"]) == (16, 6)

    def test_fresh_start_in_the_evening_starts_tomorrow(self) -> None:
        """Without having seen today's column there is nothing to put back."""
        assert self.parse_at(FRI, 2026, 10, 29, 21, 0) == ["2026-10-30T00:00:00+01:00"]

    def test_restored_today_entry_is_put_back(self) -> None:
        """Today's entry restored after a restart is shown until midnight."""
        parser = DailyForecastParser()
        parser.restore_today_forecast({"datetime": "2026-10-29T00:00:00+01:00", "temperature": 16})
        with patch(
            "custom_components.idokep_weather_and_forecast.api.dt.datetime",
            frozen_now(2026, 10, 29, 21, 0),
        ):
            result = parser.parse(parse_html(FRI))
        assert [d["datetime"] for d in result["daily_forecast"]] == [
            "2026-10-29T00:00:00+01:00",
            "2026-10-30T00:00:00+01:00",
        ]

    def test_year_change(self) -> None:
        """Dates run correctly from December into January."""
        dec = daily_col("Csütörtök<br>2026. december 31.", 2, -3)
        jan = daily_col("Péntek<br>2027. január 1.", 1, -4)
        assert self.parse_at(dec + jan, 2026, 12, 31, 12, 0) == [
            "2026-12-31T00:00:00+01:00",
            "2027-01-01T00:00:00+01:00",
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

    def _card(self, wind_class: str, tooltip: str) -> Element:
        html = f"""
        <div class="ik wide-hourly-forecast-card">
            <div class="ik hourly-wind">
                <a class="ik hover-over d-block" data-bs-content="{tooltip}">
                    <div class="ik wind {wind_class}" style="--rotateAngle:6deg"></div>
                </a>
            </div>
        </div>
        """
        return parse_html(html).find("div")

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
        no_wind = parse_html("<div></div>").find("div")
        assert HourlyForecastParser().extract_wind_strength(no_wind) is None


# ---------------------------------------------------------------------------
# Precipitation: hourly shares, daily decimals, daily probability
# ---------------------------------------------------------------------------



@pytest.mark.parametrize(
    ("text", "bar_height", "expected"),
    [
        ("5 mm", 11, 5.0),
        ("0,5 mm", None, 0.5),
        ("1.2 mm", None, 1.2),
        (".", 3, 1.0),  # "Elszórtan": no number, the 3 px bar means 1 mm
        (".", None, 0),
        ("", None, 0),
    ],
)
def test_daily_precipitation(text: str, bar_height: int | None, expected: float) -> None:
    """The printed amount (with decimals), else the bar's: (height - 1) / 2."""
    bar_html = (
        f'<div class="ik rainlevel blue" style="height: {bar_height}px;"></div>'
        if bar_height
        else ""
    )
    col = parse_html(
        f'<div class="ik dailyForecastCol"><a data-bs-content="x">'
        f'<span class="ik mm">{text}</span>{bar_html}</a></div>').find("div")
    assert DailyForecastParser().extract_precipitation(col) == expected


def _rain_card(level: str = "", meter: int | None = None) -> Element:
    """Return an hourly card with a rain bar (comment content, meter px)."""
    meter_html = (
        f'<div class="ik hourly-rainlevel-meter" style="height: {meter}px;"></div>'
        if meter is not None
        else ""
    )
    html = (
        '<div class="ik wide-hourly-forecast-card"><div class="ik '
        'hourly-rainlevel-container"><div class="ik hourly-rainlevel-container-inner">'
        f'<div class="ik hourly-rainlevel">{level}</div>{meter_html}</div></div></div>'
    )
    return parse_html(html).find("div")


@pytest.mark.parametrize(
    ("level", "meter", "expected"),
    [
        # The hidden value is in tenths of a mm, as on the page
        ("<!--2 mm-->", 12, 0.2),
        ("<!-- 7 mm -->", 16, 0.7),  # from 0.3 mm the bar stays at 16 px
        ("<!--16 mm-->", 16, 1.6),  # the page on 2026-11-19 at 0:00
        ("<!--0,5 mm-->", 7, 0.05),
        ("", 7, 0.1),  # no comment: from the bar
        ("", 12, 0.2),
        ("", 16, 0.3),
        ("", None, 0),  # no bar, no rain
        ("<!--nincs-->", None, 0),
    ],
)
def test_extract_precipitation_amount(level: str, meter: int | None, expected: float) -> None:
    """The hour's mm comes from the hidden comment (tenths), else from the bar."""
    card = _rain_card(level, meter)
    assert HourlyForecastParser().extract_precipitation_amount(card) == expected


def test_card_without_rain_bar_has_no_precipitation() -> None:
    """A card with no rain bar at all (rainlevel-na) has 0 mm."""
    card = parse_html(
        '<div class="ik wide-hourly-forecast-card"><div class="ik rainlevel-na">'
        "</div></div>").find("div")
    assert HourlyForecastParser().extract_precipitation_amount(card) == 0


# ---- remaining parser and client paths ------------------------------------------


@pytest.mark.asyncio
async def test_get_html_timeout_raises_communication_error() -> None:
    """A timeout is a communication error too (the page is retried)."""
    session = Mock(spec=aiohttp.ClientSession)
    session.get = Mock(side_effect=TimeoutError())
    with pytest.raises(IdokepApiClientCommunicationError, match="Timeout"):
        await HttpClient(session).get_html("https://www.idokep.hu/idojaras/Test")


def test_current_hour_skips_unusable_kept_items() -> None:
    """Kept items without a valid, zoned time are ignored."""
    tz = zoneinfo.ZoneInfo("Europe/Budapest")
    parser = HourlyForecastParser()
    current = {"datetime": "2026-11-03T08:00:00+01:00", "temperature": 7}
    parser.restore_hourly_forecast(
        [
            {"temperature": 1},  # no time
            {"datetime": "not a time"},
            {"datetime": "2026-11-03T08:00:00"},  # no time zone
            current,
        ]
    )
    forecast = [{"datetime": "2026-11-03T09:00:00+01:00", "temperature": 8}]
    now = dt.datetime(2026, 11, 3, 8, 30, tzinfo=tz)

    assert parser._with_current_hour(forecast, now) == [current, *forecast]


def test_hourly_card_without_temperature_is_skipped() -> None:
    """A card with an hour but no temperature gives no forecast item."""
    card = parse_html(
        '<div class="ik wide-hourly-forecast-card">'
        '<div class="ik wide-hourly-forecast-hour">9:00</div></div>').find("div")
    hour = card.find("div", class_="ik wide-hourly-forecast-hour")
    assert HourlyForecastParser()._parse_hourly_card(card, hour, dt.date.today()) is None


def test_empty_rain_bar_is_no_precipitation() -> None:
    """A 0 px rain bar means no precipitation."""
    assert HourlyForecastParser().extract_precipitation_amount(_rain_card("", 0)) == 0


def test_client_passes_restored_data_to_the_parsers() -> None:
    """Restored today and hourly items reach the daily and hourly parsers."""
    client = IdokepApiClient(Mock())
    today = {"datetime": "2026-11-03T00:00:00+01:00", "temperature": 10}
    hours = [{"datetime": "2026-11-03T08:00:00+01:00"}]
    with (
        patch.object(client._daily_parser, "restore_today_forecast") as daily,
        patch.object(client._hourly_parser, "restore_hourly_forecast") as hourly,
    ):
        client.restore_today_forecast(today)
        client.restore_hourly_forecast(hours)
    daily.assert_called_once_with(today)
    hourly.assert_called_once_with(hours)


# ---- station maps (/para, /legnyomas) ---------------------------------------------


def _humidity_area(code: str, value: int, name: str, href: str | None = None) -> str:
    link = href or f"https://www.idokep.hu/automata/{code}"
    return (
        f"<area shape='rect' coords='1,2,3,4' href='{link}' "
        f"onmouseover='this.T_WIDTH=150;Tip(\"<b>{value}%</b><br><b>{name}</b><br>"
        f"({code})<br>TSZF: 128m<br>ma 14:19<br><img src=/automata/{code}_rh.png>\")'>"
    )


def _dew_point_area(code: str, value: int, name: str) -> str:
    return (
        f"<area shape='rect' coords='1,2,3,4' href='https://www.idokep.hu/automata/{code}' "
        f"onmouseover='this.T_WIDTH=150;Tip(\"<b>{value} &deg;C</b><br><b>{name}</b><br>"
        f"({code})<br>TSZF: 128m<br>ma 14:19\")'>"
    )


def _pressure_area(code: str, value: float, name: str) -> str:
    return (
        f"<area shape='rect' coords='1,2,3,4' href='https://www.idokep.hu/automata/{code}' "
        f"onmouseover='this.T_WIDTH=350;Tip(\"{value} hPa <b>{name}</b> ({code})"
        f"<br><img src=https://www.idokep.hu/automata/{code}_p.png>\")'>"
    )


HUMIDITY_PAGE = (
    '<map name="rh">'
    + _humidity_area("jozsamet", 59, "Debrecen-Józsa")
    + _humidity_area("a", 50, "Debrecen")
    + _humidity_area("b", 55, "Debrecen")
    + _humidity_area("METAR_LHDC", 48, "Debrecen", href="#")
    + _humidity_area("a", 99, "Debrecen")  # the same station listed again
    + _humidity_area("c", 60, "Budapest XI. ker")
    + _humidity_area("d", 65, "Budapest, Csepel")
    + _humidity_area("e", 90, "Budapestfalva")
    + _humidity_area("f", 70, "S&aacute;rospatak")
    + "</map><map name=\"td\">"
    + _dew_point_area("jozsamet", 2, "Debrecen-Józsa")
    + _dew_point_area("g", -3, "Debrecen")
    + _dew_point_area("h", -4, "Debrecen")
    + "</map>"
)
PRESSURE_PAGE = (
    '<map name="legnyomasterkep">'
    + _pressure_area("jozsamet", 1029.2, "Debrecen-Józsa")
    + _pressure_area("i", 1028.1, "Pécs")
    + _pressure_area("j", 1028.4, "Pécs")
)  # no closing tag: this page's map may end with the page


@pytest.mark.parametrize(
    ("page", "map_name", "location", "expected"),
    [
        (HUMIDITY_PAGE, "rh", "Debrecen-Józsa", 59),
        (HUMIDITY_PAGE, "td", "Debrecen-Józsa", 2),
        (PRESSURE_PAGE, "legnyomasterkep", "Debrecen-Józsa", 1029.2),
        # Several stations: the average; exact names beat "Debrecen-Józsa"; a
        # repeated station counts once; a station without a page still counts
        (HUMIDITY_PAGE, "rh", "Debrecen", 51),
        (HUMIDITY_PAGE, "td", "Debrecen", -3.5),
        (PRESSURE_PAGE, "legnyomasterkep", "Pécs", 1028.25),
        # No exact name: stations named after it, not "Budapestfalva"
        (HUMIDITY_PAGE, "rh", "Budapest", 62.5),
        # Case, spaces and HTML entities don't matter
        (HUMIDITY_PAGE, "rh", " debrecen-józsa ", 59),
        (HUMIDITY_PAGE, "rh", "Sárospatak", 70),
        # No station
        (HUMIDITY_PAGE, "rh", "Hortobágy", None),
        (HUMIDITY_PAGE, "td", "Budapest", None),
    ],
)
def test_station_map_average(
    page: str, map_name: str, location: str, expected: float | None
) -> None:
    """The settlement's value is the average of its stations on that map."""
    assert station_map_average(page, map_name, location) == expected


def test_station_map_average_rounds_to_two_decimals() -> None:
    """Averages have at most 2 decimals."""
    page = (
        '<map name="rh">'
        + _humidity_area("a", 50, "Pécs")
        + _humidity_area("b", 51, "Pécs")
        + _humidity_area("c", 51, "Pécs")
        + "</map>"
    )
    assert station_map_average(page, "rh", "Pécs") == 50.67


def test_station_map_missing_is_a_failed_fetch() -> None:
    """A page without the map (redesign, error page) is retried."""
    with pytest.raises(IdokepApiClientCommunicationError, match="td"):
        station_map_average("<html></html>", "td", "Pécs")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "humidity", "dew_point"),
    [("Debrecen-Józsa", 59, 2), ("Hortobágy", None, None)],
)
async def test_client_reads_the_humidity_map(
    location: str, humidity: float | None, dew_point: float | None
) -> None:
    """Humidity and dew point come from /para, also empty without a station."""
    client = IdokepApiClient(Mock())
    with patch.object(
        client._http_client, "get_html", AsyncMock(return_value=HUMIDITY_PAGE)
    ) as get_html:
        result = await client.async_get_humidity_map(location)

    get_html.assert_awaited_once_with("https://www.idokep.hu/para")
    assert result == {"humidity": humidity, "dew_point": dew_point}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "pressure"), [("Debrecen-Józsa", 1029.2), ("Hortobágy", None)]
)
async def test_client_reads_the_pressure_map(
    location: str, pressure: float | None
) -> None:
    """Pressure comes from /legnyomas, also empty without a station."""
    client = IdokepApiClient(Mock())
    with patch.object(
        client._http_client, "get_html", AsyncMock(return_value=PRESSURE_PAGE)
    ) as get_html:
        result = await client.async_get_pressure_map(location)

    get_html.assert_awaited_once_with("https://www.idokep.hu/legnyomas")
    assert result == {"pressure": pressure}


UV_PAGE = (
    '<map name="uv">'
    "<area shape='rect' coords='1,2,3,4' href='https://www.idokep.hu/automata/bpa25' "
    "onmouseover='this.T_WIDTH=350;Tip(\" 0.5 <b>Csem&#337;</b> (bpa25)<br>"
    "<img src=https://www.idokep.hu/automata/bpa25_uv.png>\")'>"
    "<area shape='rect' coords='1,2,3,4' href='https://www.idokep.hu/automata/b' "
    "onmouseover='this.T_WIDTH=350;Tip(\" 1.0 <b>Csem&#337;</b> (b)<br>"
    "<img src=https://www.idokep.hu/automata/b_uv.png>\")'>"
    "</map>"
)


def test_uv_map_tooltips_are_read() -> None:
    """The UV tooltip starts with a space; entities in names are decoded."""
    assert station_map_average(UV_PAGE, "uv", "Csemő") == 0.75


@pytest.mark.asyncio
@pytest.mark.parametrize(("location", "uv_index"), [("Csemő", 0.75), ("Hortobágy", None)])
async def test_client_reads_the_uv_map(location: str, uv_index: float | None) -> None:
    """The UV index comes from /uv, also empty without a station."""
    client = IdokepApiClient(Mock())
    with patch.object(
        client._http_client, "get_html", AsyncMock(return_value=UV_PAGE)
    ) as get_html:
        result = await client.async_get_uv_map(location)

    get_html.assert_awaited_once_with("https://www.idokep.hu/uv")
    assert result == {"uv_index": uv_index}


# ---- the storm's hours and the alerts' 24 hours -----------------------------------


def _hourly_page(conditions: dict[str, str]) -> Element:
    """Return a forecast page with an hourly card per "hour": condition text."""
    cards = "".join(
        f'''<div class="ik wide-hourly-forecast-card">
            <div class="ik wide-hourly-forecast-hour">{hour}</div>
            <div class="forecast-icon-container"><a data-bs-content="{text}"></a></div>
            <div class="ik tempValue"><a>5</a></div>
        </div>'''
        for hour, text in conditions.items()
    )
    return parse_html(cards)


def _parse_hourly_at(soup: Element, *when: int) -> dict:
    """Parse the hourly cards as if it were the given time."""
    now = dt.datetime(*when, tzinfo=zoneinfo.ZoneInfo("Europe/Budapest"))

    class Clock(dt.datetime):
        """The real datetime class, stopped at the given time."""

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return now.astimezone(tz)

    with patch("custom_components.idokep_weather_and_forecast.api.dt.datetime", Clock):
        return HourlyForecastParser().parse(soup)


@pytest.mark.parametrize(
    "text",
    ["Zivatar", "Száraz zivatar", "Villámlás", "Jégeső", "Eső viharos széllel", "Porvihar"],
)
@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (("14:00", "15:00", "16:00"), True),  # the current hour
        (("15:00", "16:00", "17:00"), True),  # the next hour
        (("16:00", "17:00", "18:00"), False),  # later
    ],
)
def test_storm_expected_in_the_current_or_next_hour(
    text: str, hours: tuple[str, ...], expected: bool
) -> None:
    """Each storm condition counts, in the current or next hour only."""
    conditions = dict.fromkeys(("14:00", "15:00", "16:00", "17:00"), "Napos")
    conditions[hours[0]] = text
    result = _parse_hourly_at(_hourly_page(conditions), 2026, 11, 6, 14, 30)
    assert result["storm_expected"] is expected


def test_no_storm_without_storm_conditions() -> None:
    """Rain alone isn't a storm."""
    page = _hourly_page({"14:00": "Eső", "15:00": "Erős eső"})
    assert _parse_hourly_at(page, 2026, 11, 6, 14, 30)["storm_expected"] is False


def test_storm_mark_is_only_on_stormy_hours() -> None:
    """The internal mark is set only where the condition is a storm."""
    page = _hourly_page({"14:00": "Porvihar", "15:00": "Napos"})
    forecast = _parse_hourly_at(page, 2026, 11, 6, 14, 30)["hourly_forecast"]
    assert forecast[0]["storm"] is True
    assert "storm" not in forecast[1]


def _alert_cards(alert_hour: int, count: int = 36) -> Element:
    """Return hourly alert blocks like the page's, with an alert in one of them."""
    alert = (
        '<a class="hover-over" data-bs-content="Piros riasztás szél miatt"></a>'
    )
    blocks = "".join(
        f'<div class="ik genericHourlyAlert">{alert if i == alert_hour else ""}</div>'
        for i in range(count)
    )
    return parse_html(blocks)


@pytest.mark.parametrize(
    ("alert_hour", "counted"), [(0, True), (2, True), (3, False), (35, False)]
)
def test_alerts_of_the_first_3_cards_count(alert_hour: int, counted: bool) -> None:
    """An alert on a card after the first 3 is left out."""
    result = AlertParser().parse(_alert_cards(alert_hour))
    assert result["alerts"] == ([{"type": "wind", "level": "red"}] if counted else [])


def test_wind_speed_and_gust_of_each_level() -> None:
    """Speed: the Beaufort range's lower end; gust: that speed times 1.75."""
    assert WindStrengthMapper.SPEEDS == {
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
    assert WindStrengthMapper.GUSTS == {
        level: speed * 1.75 for level, speed in WindStrengthMapper.SPEEDS.items()
    }
    assert WindStrengthMapper.GUSTS["moderate_breeze"] == 35.0


# ---- night hours ------------------------------------------------------------------


def _condition_card(text: str | None, icon: str | None) -> Element:
    """Return an hourly card with a condition text and an icon file."""
    link = (
        ""
        if text is None
        else f'<a data-bs-content="{text}">'
        + (f'<img src="/assets/forecastIcons/{icon}.svg">' if icon else "")
        + "</a>"
    )
    return parse_html(
        f'<div class="ik wide-hourly-forecast-card">'
        f'<div class="forecast-icon-container">{link}</div></div>').find("div")


@pytest.mark.parametrize(
    ("text", "icon", "expected"),
    [
        ("derült", "310", "clear-night"),  # as on the page at night
        ("derült", "010", "sunny"),  # by day
        ("derült", None, "sunny"),  # no icon: not known to be night
        ("gyengén felhős", "321", "partlycloudy"),  # HA has no night variant
        ("gyengén felhős", "021", "partlycloudy"),
        ("Napos", "310", "clear-night"),
        (None, None, None),
    ],
)
def test_hourly_condition_at_night(
    text: str | None, icon: str | None, expected: str | None
) -> None:
    """A clear hour with the page's night icon is clear-night, not sunny."""
    card = _condition_card(text, icon)
    assert HourlyForecastParser().extract_condition(card) == expected


def test_clear_night_is_a_known_condition() -> None:
    """The Condition sensor's options include it, though no page text maps to it."""
    assert "clear-night" in WeatherConditionMapper.conditions()


# ---- cloud coverage -----------------------------------------------------------------


def test_every_description_has_a_cloud_coverage() -> None:
    """Each known description has an estimate of 0, 25, 50, 75 or 100 %."""
    mapper = WeatherConditionMapper
    assert set(mapper._CLOUD_COVERAGE) == set(mapper._CONDITION_MAPPING)
    assert set(mapper._CLOUD_COVERAGE.values()) <= {0, 25, 50, 75, 100}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Derült", 0),
        ("gyengén felhős", 25),
        ("közepesen felhős", 50),
        ("erősen felhős", 75),
        ("borult", 100),
        (" Eső ", 100),
        ("zivatar", 75),
        ("hózivatar", 75),
        ("intenzív eső", 100),
        ("párás", 50),
        ("ismeretlen", None),
        (None, None),
    ],
)
def test_cloud_coverage(text: str | None, expected: int | None) -> None:
    """The estimate comes from the Hungarian text, any case."""
    assert WeatherConditionMapper.cloud_coverage(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"), [("gyengén felhős", 25), ("hózápor", 75), (None, None)]
)
def test_hourly_cloud_coverage(text: str | None, expected: int | None) -> None:
    """Each hour has the cloud coverage of its description."""
    link = "" if text is None else f'<a data-bs-content="{text}"></a>'
    card = parse_html(
        '<div class="ik wide-hourly-forecast-card">'
        '<div class="ik wide-hourly-forecast-hour">14:00</div>'
        '<div class="ik tempValue"><a>5</a></div>'
        f'<div class="forecast-icon-container">{link}</div></div>').find("div")
    hour = card.find("div", class_="ik wide-hourly-forecast-hour")
    item = HourlyForecastParser()._parse_hourly_card(card, hour, dt.date(2026, 11, 17))
    assert item["cloud_coverage"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zivatar", True),
        ("Hózivatar", False),
        ("Intenzív eső", False),
        ("Villámlás", True),
        ("Jégeső", True),
        ("Eső viharos széllel", True),
        ("Eső", False),
        ("Napos", False),
    ],
)
def test_current_weather_marks_a_storm_now(text: str, expected: bool) -> None:
    """The current condition's text tells whether it's stormy now."""
    soup = parse_html(
        f'<div class="current-temperature">12 °C</div>'
        f'<div class="current-weather">{text}</div>')
    assert CurrentWeatherParser().parse(soup)["storm_now"] is expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (dt.date(2026, 11, 19), "2026-11-19T00:00:00+01:00"),  # winter time
        (dt.date(2026, 7, 1), "2026-07-01T00:00:00+02:00"),  # summer time
        (dt.date(2026, 3, 29), "2026-03-29T00:00:00+01:00"),  # switches at 2:00
    ],
)
def test_daily_datetime_is_local_midnight(day: dt.date, expected: str) -> None:
    """A daily item's datetime is the day's local midnight with its offset."""
    assert TimeUtils.day_start(day) == expected


@pytest.mark.parametrize(
    ("now", "hours", "first"),
    [
        # After 23:00 the list starts with tomorrow's 0:00 (the page on
        # 2026-11-19 at 23:58)
        ((2026, 11, 19, 23, 58), ("0:00", "1:00"), "2026-11-20T00:00:00+01:00"),
        ((2026, 11, 19, 23, 1), ("0:00", "1:00"), "2026-11-20T00:00:00+01:00"),
        # A page cached a little earlier still starting with the current hour
        ((2026, 11, 19, 23, 58), ("23:00", "0:00"), "2026-11-19T23:00:00+01:00"),
        # Just after midnight and in the day: the first hour is today's
        ((2026, 11, 20, 0, 5), ("1:00", "2:00"), "2026-11-20T01:00:00+01:00"),
        ((2026, 11, 19, 12, 30), ("13:00", "14:00"), "2026-11-19T13:00:00+01:00"),
    ],
)
def test_hourly_list_starts_on_the_right_day(
    now: tuple[int, ...], hours: tuple[str, ...], first: str
) -> None:
    """The first card's date follows the clock, also across midnight."""
    page = _hourly_page(dict.fromkeys(hours, "Eső"))
    forecast = _parse_hourly_at(page, *now)["hourly_forecast"]
    starts = [item["datetime"] for item in forecast]
    assert first in starts
    assert starts[starts.index(first) + 1] == (
        dt.datetime.fromisoformat(first) + dt.timedelta(hours=1)
    ).isoformat()


def _daily_col(alt: str, amount: str | None) -> Element:
    """Return a daily column with a condition and optionally an amount + bar."""
    rain = (
        '<div class="ik rainlevel-container"><a>'
        f'<span class="ik mm">{amount}</span>'
        '<div class="ik rainlevel blue" style="height: 5px;"></div></a></div>'
        if amount is not None
        else ""
    )
    html = (
        '<div class="ik dailyForecastCol"><div class="ik dfIconAlert">'
        '<a title="Hétfő<br>2026. december 7." data-bs-content="'
        "&lt;img class='ik popover-icon' src='/assets/forecastIcons/x.svg' "
        f"alt='{alt}'&gt;\"></a></div>{rain}</div>"
    )
    col = parse_html(html).find("div", "dailyForecastCol")
    assert col is not None
    return col


@pytest.mark.parametrize(
    ("alt", "amount", "expected"),
    [
        # Later days show no amount at all: a rainy or snowy one is unknown
        ("eső", None, None),
        ("havazás", None, None),
        ("havas eső", None, None),
        # ... while a dry one (like a dry day nearby) stays 0 mm
        ("borult", None, 0),
        ("derült", None, 0),
        # A shown amount is used as before
        ("eső", "2 mm", 2.0),
        ("havazás", "2 cm", 2.0),
    ],
)
def test_daily_precipitation_unknown_on_later_wet_days(
    alt: str, amount: str | None, expected: float | None
) -> None:
    """A wet day without an amount on the page has an unknown amount (None)."""
    item = DailyForecastParser()._parse_daily_column(
        _daily_col(alt, amount), dt.date(2026, 12, 7)
    )
    assert item["precipitation"] == expected
