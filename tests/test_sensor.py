"""Tests for the Idokep sensor platform."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant

from custom_components.idokep_weather_and_forecast.api import (
    WeatherConditionMapper,
    WindStrengthMapper,
)
from custom_components.idokep_weather_and_forecast.sensor import (
    COMPASS_POINTS,
    ENTITY_DESCRIPTIONS,
    IdokepSensor,
    async_setup_entry,
    bearing_to_compass,
)

TRANSLATIONS_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "idokep_weather_and_forecast"
    / "translations"
)


class TestIdokepSensorEntityDescriptions:
    """Test the sensor entity descriptions."""

    def test_entity_descriptions_count(self) -> None:
        """Test that we have the expected number of entity descriptions."""
        assert len(ENTITY_DESCRIPTIONS) == 18

    def test_entity_descriptions_keys(self) -> None:
        """Test that entity descriptions have the expected keys."""
        expected_keys = {
            "temperature",
            "temperature_max_today",
            "temperature_min_today",
            "condition",
            "wind_bearing",
            "wind_direction",
            "wind_strength",
            "wind_speed",
            "precipitation",
            "precipitation_probability",
            "humidity",
            "dew_point",
            "pressure",
            "uv_index",
            "alert_level",
            "warning_level",
            "apparent_temperature",
            "wind_gust_speed",
        }
        actual_keys = {desc.key for desc in ENTITY_DESCRIPTIONS}
        assert actual_keys == expected_keys

    @pytest.mark.parametrize(
        ("key", "device_class", "unit"),
        [
            ("humidity", SensorDeviceClass.HUMIDITY, "%"),
            ("dew_point", SensorDeviceClass.TEMPERATURE, "°C"),
            ("apparent_temperature", SensorDeviceClass.TEMPERATURE, "°C"),
            ("wind_gust_speed", SensorDeviceClass.WIND_SPEED, "km/h"),
            ("pressure", SensorDeviceClass.ATMOSPHERIC_PRESSURE, "hPa"),
            ("uv_index", None, "UV index"),
        ],
    )
    def test_station_map_descriptions(
        self, key: str, device_class: SensorDeviceClass | None, unit: str
    ) -> None:
        """The values from the maps, shown with 1 decimal."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == key)
        assert desc.device_class == device_class
        assert desc.native_unit_of_measurement == unit
        assert desc.suggested_display_precision == 1
        assert desc.state_class is None

    @pytest.mark.parametrize(
        ("data", "expected"),
        [({}, None), ({"warning_level": "none"}, "none"), ({"warning_level": "red"}, "red")],
    )
    def test_warning_level(
        self, mock_coordinator: Mock, data: dict, expected: str | None
    ) -> None:
        """The banner's level; unknown until the forecast page is read."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "warning_level")
        assert desc.device_class == SensorDeviceClass.ENUM
        assert desc.options == ["none", "yellow", "orange", "red"]
        mock_coordinator.data = data
        assert IdokepSensor(mock_coordinator, desc).native_value == expected

    @pytest.mark.parametrize(
        ("alerts_by_level", "expected"),
        [
            (None, None),  # the forecast page isn't read yet: unknown
            ({}, "none"),
            ({"yellow": [], "orange": [], "red": []}, "none"),
            ({"yellow": [{"type": "fog"}], "orange": [], "red": []}, "yellow"),
            ({"yellow": [{"type": "fog"}], "orange": [{"type": "wind"}]}, "orange"),
            ({"yellow": [{}], "orange": [{}], "red": [{}]}, "red"),
        ],
    )
    def test_alert_level_is_the_highest(
        self, mock_coordinator: Mock, alerts_by_level: dict | None, expected: str | None
    ) -> None:
        """The state is the highest level among the alerts; it has no attributes."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "alert_level")
        assert desc.device_class == SensorDeviceClass.ENUM
        assert desc.options == ["none", "yellow", "orange", "red"]
        mock_coordinator.data = (
            {} if alerts_by_level is None else {"alerts_by_level": alerts_by_level}
        )
        sensor = IdokepSensor(mock_coordinator, desc)
        assert sensor.native_value == expected
        assert sensor.extra_state_attributes is None

    @pytest.mark.parametrize(
        "key", ["humidity", "dew_point", "apparent_temperature", "pressure", "uv_index"]
    )
    def test_map_sensor_unavailable_without_value(
        self, mock_coordinator: Mock, key: str
    ) -> None:
        """A settlement without a station: unavailable, not unknown."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == key)
        mock_coordinator.last_update_success = True

        mock_coordinator.data = {key: 12.5}
        assert IdokepSensor(mock_coordinator, desc).available

        mock_coordinator.data = {key: 0}  # 0 is a value
        assert IdokepSensor(mock_coordinator, desc).available

        mock_coordinator.data = {key: None}
        assert not IdokepSensor(mock_coordinator, desc).available

        mock_coordinator.data = {}  # the page was never read
        assert not IdokepSensor(mock_coordinator, desc).available

        mock_coordinator.data = {key: 12.5}
        mock_coordinator.last_update_success = False
        assert not IdokepSensor(mock_coordinator, desc).available

    def test_wind_gust_sensor_shows_the_estimate(self, mock_coordinator: Mock) -> None:
        """The value is the parser's gust; without one it is unknown, not unavailable."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_gust_speed")
        mock_coordinator.last_update_success = True
        mock_coordinator.data = {"wind_gust_speed": 28}
        assert IdokepSensor(mock_coordinator, desc).native_value == 28

        mock_coordinator.data = {}
        sensor = IdokepSensor(mock_coordinator, desc)
        assert sensor.native_value is None
        assert sensor.available

    def test_other_sensors_stay_available_without_value(
        self, mock_coordinator: Mock
    ) -> None:
        """The other sensors keep showing unknown, as before."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_speed")
        mock_coordinator.last_update_success = True
        mock_coordinator.data = {}
        assert IdokepSensor(mock_coordinator, desc).available

    def test_precipitation_description(self) -> None:
        """Millimetres with the precipitation device class, no state class."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "precipitation")
        assert desc.translation_key == "precipitation"
        assert desc.native_unit_of_measurement == "mm"
        assert desc.device_class == SensorDeviceClass.PRECIPITATION
        assert desc.state_class is None

    def test_precipitation_probability_description(self) -> None:
        """Percent, no device class or state class."""
        desc = next(
            d for d in ENTITY_DESCRIPTIONS if d.key == "precipitation_probability"
        )
        assert desc.translation_key == "precipitation_probability"
        assert desc.native_unit_of_measurement == "%"
        assert desc.device_class is None
        assert desc.state_class is None

    def test_temperature_description(self) -> None:
        """Test the temperature entity description."""
        temp_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "temperature"
        )
        assert temp_desc.key == "temperature"
        assert temp_desc.translation_key == "temperature"
        assert temp_desc.native_unit_of_measurement == "°C"
        assert temp_desc.device_class == SensorDeviceClass.TEMPERATURE
        assert temp_desc.state_class is None

    def test_condition_descriptions(self) -> None:
        """Test the condition entity description."""
        condition_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "condition"
        )
        assert condition_desc.key == "condition"
        assert condition_desc.translation_key == "condition"
        assert condition_desc.device_class == SensorDeviceClass.ENUM
        assert condition_desc.options == WeatherConditionMapper.conditions()
        assert "unknown" not in condition_desc.options

    @pytest.mark.parametrize("lang", ["en", "hu"])
    def test_condition_states_have_friendly_names(self, lang: str) -> None:
        """Every condition option has a translated name in each language."""
        path = TRANSLATIONS_DIR / f"{lang}.json"
        translations = json.loads(path.read_text(encoding="utf-8"))
        states = translations["entity"]["sensor"]["condition"]["state"]
        condition_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "condition"
        )
        assert set(states) == set(condition_desc.options)
        assert all(name.strip() for name in states.values())

    def test_condition_names_kept_utf8(self) -> None:
        """Hungarian accents survive in the translation file."""
        path = TRANSLATIONS_DIR / "hu.json"
        translations = json.loads(path.read_text(encoding="utf-8"))
        sensor = translations["entity"]["sensor"]
        assert sensor["condition"]["name"] == "Állapot"
        assert sensor["condition"]["state"]["partlycloudy"] == "Részben felhős"

    def test_wind_bearing_description(self) -> None:
        """Test the wind direction entity description."""
        wind_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "wind_bearing"
        )
        assert wind_desc.translation_key == "wind_bearing"
        assert wind_desc.native_unit_of_measurement == "°"
        assert wind_desc.device_class == SensorDeviceClass.WIND_DIRECTION
        assert wind_desc.state_class is None


class TestAsyncSetupEntry:
    """Test the async_setup_entry function."""

    @pytest.mark.asyncio
    async def test_async_setup_entry_creates_all_sensors(self) -> None:
        """Test that async_setup_entry creates all expected sensors."""
        hass = Mock(spec=HomeAssistant)
        entry = Mock()
        entry.runtime_data.coordinator = Mock()
        entry.runtime_data.coordinator.config_entry.data = {"location": "Budapest"}
        entry.runtime_data.coordinator.config_entry.entry_id = "test_entry"
        entry.runtime_data.station = None
        async_add_entities = Mock()

        await async_setup_entry(hass, entry, async_add_entities)

        # Check that async_add_entities was called with the correct number of sensors
        async_add_entities.assert_called_once()
        sensors = list(async_add_entities.call_args[0][0])
        assert len(sensors) == len(ENTITY_DESCRIPTIONS)

        # Check that all sensors are IdokepSensor instances
        for sensor in sensors:
            assert isinstance(sensor, IdokepSensor)


class TestIdokepSensor:
    """Test the IdokepSensor class."""

    def test_has_entity_name(self) -> None:
        """Test that has_entity_name returns True."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        entity_desc = SensorEntityDescription(key="test", translation_key="test")
        sensor = IdokepSensor(coordinator, entity_desc)
        assert sensor.has_entity_name is True

    def test_initialization(self) -> None:
        """Test sensor initialization."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.config_entry.entry_id = "test_entry"
        entity_desc = SensorEntityDescription(
            key="temperature", translation_key="temperature"
        )

        sensor = IdokepSensor(coordinator, entity_desc)

        assert sensor.coordinator == coordinator
        assert sensor.entity_description == entity_desc
        assert sensor._attr_unique_id == "test_entry_temperature"

    def test_native_value_condition_in_options(self) -> None:
        """A known condition is passed through for the frontend to translate."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"condition": "partlycloudy"}
        condition_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "condition"
        )
        sensor = IdokepSensor(coordinator, condition_desc)
        assert sensor.native_value == "partlycloudy"

    @pytest.mark.parametrize(
        ("condition", "sun_up", "expected"),
        [
            ("sunny", True, "sunny"),
            ("sunny", False, "clear-night"),
            ("cloudy", False, "cloudy"),
            ("cloudy", True, "cloudy"),
        ],
    )
    def test_condition_at_night(
        self, condition: str, sun_up: bool, expected: str
    ) -> None:
        """Sunny shows as clear-night while the sun is down."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"condition": condition}
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "condition")
        sensor = IdokepSensor(coordinator, desc)
        with patch(
            "custom_components.idokep_weather_and_forecast.sensor.sun.is_up",
            return_value=sun_up,
        ):
            assert sensor.native_value == expected

    def test_native_value_unmapped_condition_is_none(self) -> None:
        """An unmapped ("unknown") condition becomes None, not an invalid option."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"condition": "unknown"}
        condition_desc = next(
            desc for desc in ENTITY_DESCRIPTIONS if desc.key == "condition"
        )
        sensor = IdokepSensor(coordinator, condition_desc)
        assert sensor.native_value is None

    def test_native_value_string(self) -> None:
        """Test native_value with string data."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"temperature": "20.5"}
        entity_desc = SensorEntityDescription(
            key="temperature", translation_key="temperature"
        )

        sensor = IdokepSensor(coordinator, entity_desc)
        assert sensor.native_value == "20.5"

    def test_native_value_number(self) -> None:
        """Test native_value with numeric data."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"temperature": 20.5}
        entity_desc = SensorEntityDescription(
            key="temperature", translation_key="temperature"
        )

        sensor = IdokepSensor(coordinator, entity_desc)
        assert sensor.native_value == 20.5

    def test_native_value_none(self) -> None:
        """Test native_value when data is missing."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {}
        entity_desc = SensorEntityDescription(
            key="temperature", translation_key="temperature"
        )

        sensor = IdokepSensor(coordinator, entity_desc)
        assert sensor.native_value is None


class TestWindDirection:
    """The compass-point Wind direction sensor."""

    @pytest.mark.parametrize(
        ("bearing", "expected"),
        [
            (0, "n"),
            (11, "n"),  # N covers 348.75-11.25
            (12, "nne"),
            (22, "nne"),
            (34, "ne"),  # NNE covers 11.25-33.75
            (45, "ne"),
            (68, "ene"),
            (90, "e"),
            (112, "ese"),
            (135, "se"),
            (158, "sse"),
            (180, "s"),
            (202, "ssw"),
            (225, "sw"),
            (248, "wsw"),
            (270, "w"),
            (292, "wnw"),
            (315, "nw"),
            (337, "nnw"),
            (348, "nnw"),
            (349, "n"),
            (359, "n"),
            (360, "n"),
        ],
    )
    def test_bearing_to_compass(self, bearing: int, expected: str) -> None:
        """Each compass point covers 22.5 degrees centred on it."""
        assert bearing_to_compass(bearing) == expected

    def test_no_bearing(self) -> None:
        """Without a bearing there is no direction."""
        assert bearing_to_compass(None) is None

    def test_description(self) -> None:
        """Enum sensor whose options are the 16 compass points."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_direction")
        assert desc.device_class == SensorDeviceClass.ENUM
        assert desc.options == [
            "n", "nne", "ne", "ene", "e", "ese", "se", "sse",
            "s", "ssw", "sw", "wsw", "w", "wnw", "nw", "nnw",
        ]  # fmt: skip

    def test_native_value_from_wind_bearing(self) -> None:
        """The sensor reads the wind bearing and returns its compass point."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"wind_bearing": 200}
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_direction")
        assert IdokepSensor(coordinator, desc).native_value == "ssw"

        coordinator.data = {}
        assert IdokepSensor(coordinator, desc).native_value is None

    @pytest.mark.parametrize(
        ("lang", "expected"),
        [
            ("en", {"n": "N", "nne": "NNE", "sw": "SW", "wnw": "WNW"}),
            ("hu", {"n": "É", "nne": "ÉÉK", "sw": "DNy", "wnw": "NyÉNy"}),
        ],
    )
    def test_translated_names(self, lang: str, expected: dict[str, str]) -> None:
        """Every compass point has a short translated name."""
        path = TRANSLATIONS_DIR / f"{lang}.json"
        translations = json.loads(path.read_text(encoding="utf-8"))
        states = translations["entity"]["sensor"]["wind_direction"]["state"]
        assert set(states) == set(COMPASS_POINTS)
        assert len(set(states.values())) == len(COMPASS_POINTS)  # all distinct
        for code, name in expected.items():
            assert states[code] == name


class TestTodayMinMaxSensors:
    """Today's forecast max/min temperature sensors."""

    @pytest.mark.parametrize("key", ["temperature_max_today", "temperature_min_today"])
    def test_description(self, key: str) -> None:
        """Temperature sensors without a state class (forecasts, not measurements)."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == key)
        assert desc.translation_key == key
        assert desc.device_class == SensorDeviceClass.TEMPERATURE
        assert desc.native_unit_of_measurement == "°C"
        assert desc.state_class is None

    def test_values(self) -> None:
        """The sensors show the parsed values, Unknown when missing."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"temperature_max_today": 16, "temperature_min_today": 6}
        max_desc = next(
            d for d in ENTITY_DESCRIPTIONS if d.key == "temperature_max_today"
        )
        min_desc = next(
            d for d in ENTITY_DESCRIPTIONS if d.key == "temperature_min_today"
        )
        assert IdokepSensor(coordinator, max_desc).native_value == 16
        assert IdokepSensor(coordinator, min_desc).native_value == 6

        coordinator.data = {}
        assert IdokepSensor(coordinator, max_desc).native_value is None


class TestWindStrengthSensors:
    """Wind strength and Wind speed (estimated from the strength)."""

    def test_wind_strength_description(self) -> None:
        """Enum sensor of the 13 Beaufort levels."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_strength")
        assert desc.device_class == SensorDeviceClass.ENUM
        assert desc.options == WindStrengthMapper.levels()
        assert len(desc.options) == 13

    def test_wind_speed_description(self) -> None:
        """km/h wind speed, without statistics (an estimate)."""
        desc = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_speed")
        assert desc.device_class == SensorDeviceClass.WIND_SPEED
        assert desc.native_unit_of_measurement == "km/h"
        assert desc.state_class is None

    @pytest.mark.parametrize(
        ("lang", "name", "speed_name", "moderate"),
        [
            ("en", "Wind strength", "Wind speed", "Moderate breeze"),
            ("hu", "Szélerősség", "Szélsebesség", "Mérsékelt szél"),
        ],
    )
    def test_translations(
        self, lang: str, name: str, speed_name: str, moderate: str
    ) -> None:
        """Both sensors are named and every level has a translated name."""
        path = TRANSLATIONS_DIR / f"{lang}.json"
        sensors = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        assert sensors["wind_strength"]["name"] == name
        assert sensors["wind_speed"]["name"] == speed_name
        states = sensors["wind_strength"]["state"]
        assert set(states) == set(WindStrengthMapper.levels())
        assert len(set(states.values())) == len(states)  # all distinct
        assert states["moderate_breeze"] == moderate

    @pytest.mark.parametrize(
        ("lang", "expected"),
        [
            (
                "hu",
                {
                    "light_air": "Alig érezhető szél",
                    "light_breeze": "Könnyű szél",
                    "strong_gale": "Heves vihar",
                    "storm": "Dühöngő vihar",
                    "violent_storm": "Heves szélvihar",
                    "hurricane": "Orkán",
                },
            ),
            (
                "en",
                {
                    "near_gale": "High wind / Moderate gale",
                    "gale": "Gale / Fresh gale",
                    "storm": "Storm / Whole gale",
                    "hurricane": "Hurricane force",
                },
            ),
        ],
    )
    def test_official_names(self, lang: str, expected: dict[str, str]) -> None:
        """The official Hungarian and WMO English Beaufort names are used."""
        path = TRANSLATIONS_DIR / f"{lang}.json"
        sensors = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        states = sensors["wind_strength"]["state"]
        for code, name in expected.items():
            assert states[code] == name

    def test_values(self) -> None:
        """The sensors show the current hour's strength and estimate."""
        coordinator = Mock()
        coordinator.config_entry.data = {"location": "Budapest"}
        coordinator.data = {"wind_strength": "moderate_breeze", "wind_speed": 24}
        strength = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_strength")
        speed = next(d for d in ENTITY_DESCRIPTIONS if d.key == "wind_speed")
        assert IdokepSensor(coordinator, strength).native_value == "moderate_breeze"
        assert IdokepSensor(coordinator, speed).native_value == 24

        coordinator.data = {}
        assert IdokepSensor(coordinator, strength).native_value is None
        assert IdokepSensor(coordinator, speed).native_value is None
