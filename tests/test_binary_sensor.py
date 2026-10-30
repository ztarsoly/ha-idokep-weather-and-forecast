"""Unit tests for the Időkép binary sensor platform."""

from __future__ import annotations

import datetime
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant

from custom_components.idokep_weather_and_forecast.api import AlertData, TimeUtils
from custom_components.idokep_weather_and_forecast.binary_sensor import (
    ENTITY_DESCRIPTIONS,
    IdokepBinarySensor,
    async_setup_entry,
)

DESCRIPTIONS = {d.key: d for d in ENTITY_DESCRIPTIONS}


class TestIdokepBinarySensor:
    """Test cases for IdokepBinarySensor."""

    def test_entity_descriptions(self) -> None:
        """Test that ENTITY_DESCRIPTIONS is properly defined."""
        assert list(DESCRIPTIONS) == [
            "storm_expected_1h",
            "weather_alert",
            "alert_yellow",
            "alert_orange",
            "alert_red",
        ]
        for description in ENTITY_DESCRIPTIONS:
            assert description.device_class == BinarySensorDeviceClass.SAFETY

    @pytest.mark.asyncio
    async def test_async_setup_entry(self) -> None:
        """Test the setup of binary sensor entities."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        mock_entry = Mock()
        mock_coordinator = Mock()
        mock_entry.runtime_data.coordinator = mock_coordinator
        mock_async_add_entities = Mock()

        # Call the setup function
        await async_setup_entry(mock_hass, mock_entry, mock_async_add_entities)

        # Verify that async_add_entities was called
        mock_async_add_entities.assert_called_once()

        # Get the entities that were added
        added_entities = list(mock_async_add_entities.call_args[0][0])
        assert len(added_entities) == len(ENTITY_DESCRIPTIONS)

        # Verify the entity is of correct type
        entity = added_entities[0]
        assert isinstance(entity, IdokepBinarySensor)

    def test_binary_sensor_initialization(self, mock_coordinator: Mock) -> None:
        """Test initialization of binary sensor."""
        entity_description = DESCRIPTIONS["storm_expected_1h"]

        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        # Verify initialization
        assert binary_sensor.coordinator is mock_coordinator
        assert binary_sensor.entity_description is entity_description
        assert binary_sensor.entity_description.key == "storm_expected_1h"

    def test_binary_sensor_inheritance(self, mock_coordinator: Mock) -> None:
        """Test that binary sensor inherits from correct base classes."""
        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        # Verify inheritance - basic checks
        assert isinstance(binary_sensor, IdokepBinarySensor)
        assert hasattr(binary_sensor, "coordinator")

    def test_alert_sensors_is_on(self, mock_coordinator: Mock) -> None:
        """Test alert sensors is_on with alerts present."""
        # Setup coordinator data with alerts
        mock_coordinator.data = {
            "alerts": [
                AlertData(
                    level="yellow",
                    type="wind",
                    description="Sárga riasztás szélre",
                    icon_url=None,
                ),
                AlertData(
                    level="orange",
                    type="thunderstorm",
                    description="Narancs riasztás zivatar",
                    icon_url=None,
                ),
            ],
            "alerts_by_level": {
                "yellow": [
                    {
                        "type": "wind",
                        "description": "Sárga riasztás szélre",
                        "icon_url": None,
                    }
                ],
                "orange": [
                    {
                        "type": "thunderstorm",
                        "description": "Narancs riasztás zivatar",
                        "icon_url": None,
                    }
                ],
                "red": [],
            },
        }

        # Test weather_alert sensor
        entity_description = DESCRIPTIONS["weather_alert"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is True

        # Test alert_yellow sensor
        entity_description = DESCRIPTIONS["alert_yellow"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is True

        # Test alert_orange sensor
        entity_description = DESCRIPTIONS["alert_orange"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is True

        # Test alert_red sensor (should be off)
        entity_description = DESCRIPTIONS["alert_red"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is False

    def test_alert_sensors_is_off_no_alerts(self, mock_coordinator: Mock) -> None:
        """Test alert sensors is_on with no alerts."""
        # Setup coordinator data without alerts
        mock_coordinator.data = {
            "alerts": [],
            "alerts_by_level": {
                "yellow": [],
                "orange": [],
                "red": [],
            },
        }

        # Test weather_alert sensor
        entity_description = DESCRIPTIONS["weather_alert"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is False

        # Test alert_yellow sensor
        entity_description = DESCRIPTIONS["alert_yellow"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )
        assert binary_sensor.is_on is False

    def test_weather_alert_extra_state_attributes(self, mock_coordinator: Mock) -> None:
        """Test extra_state_attributes for weather_alert sensor."""
        # Setup coordinator data with alerts
        mock_coordinator.data = {
            "alerts": [
                AlertData(
                    level="yellow",
                    type="wind",
                    description="Sárga riasztás szélre",
                    icon_url="https://www.idokep.hu/images/wind.png",
                ),
            ],
            "alerts_by_level": {
                "yellow": [
                    {
                        "type": "wind",
                        "description": "Sárga riasztás szélre",
                        "icon_url": "https://www.idokep.hu/images/wind.png",
                    }
                ],
                "orange": [],
                "red": [],
            },
        }

        entity_description = DESCRIPTIONS["weather_alert"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        attrs = binary_sensor.extra_state_attributes
        assert attrs["alert_count"] == 1
        assert attrs["yellow_alerts"] == 1
        assert attrs["orange_alerts"] == 0
        assert attrs["red_alerts"] == 0
        assert len(attrs["alerts"]) == 1
        assert attrs["alerts"][0]["level"] == "yellow"
        assert attrs["alerts"][0]["type"] == "wind"

    def test_level_alert_extra_state_attributes(self, mock_coordinator: Mock) -> None:
        """Test extra_state_attributes for level-specific alert sensors."""
        # Setup coordinator data with alerts
        mock_coordinator.data = {
            "alerts_by_level": {
                "yellow": [
                    {
                        "type": "wind",
                        "description": "Sárga riasztás szélre",
                        "icon_url": None,
                    },
                    {
                        "type": "fog",
                        "description": "Sárga riasztás ködre",
                        "icon_url": None,
                    },
                ],
                "orange": [],
                "red": [],
            },
        }

        entity_description = DESCRIPTIONS["alert_yellow"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        attrs = binary_sensor.extra_state_attributes
        assert attrs["alert_count"] == 2
        assert len(attrs["alerts"]) == 2
        assert attrs["alerts"][0]["type"] == "wind"
        assert attrs["alerts"][1]["type"] == "fog"

    def test_extra_state_attributes_no_alerts(self, mock_coordinator: Mock) -> None:
        """Test extra_state_attributes with no alerts."""
        # Setup coordinator data without alerts
        mock_coordinator.data = {
            "alerts": [],
            "alerts_by_level": {
                "yellow": [],
                "orange": [],
                "red": [],
            },
        }

        entity_description = DESCRIPTIONS["weather_alert"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        attrs = binary_sensor.extra_state_attributes
        assert attrs["alert_count"] == 0
        assert attrs["yellow_alerts"] == 0
        assert attrs["orange_alerts"] == 0
        assert attrs["red_alerts"] == 0
        assert len(attrs["alerts"]) == 0

    def test_extra_state_attributes_non_alert_sensor(
        self, mock_coordinator: Mock
    ) -> None:
        """Test extra_state_attributes for non-alert sensors returns empty dict."""
        mock_coordinator.data = {"temperature": 20}

        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        attrs = binary_sensor.extra_state_attributes
        assert not attrs

    def test_storm_expected_next_hour_is_on(self, mock_coordinator: Mock) -> None:
        """Test storm_expected_1h sensor with storm in forecast."""
        now = datetime.datetime.now(datetime.UTC)
        next_30_min = now + datetime.timedelta(minutes=30)

        # Setup coordinator data with storm forecast
        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": next_30_min.isoformat(),
                    "condition": "lightning-rainy",
                    "temperature": 20,
                }
            ]
        }

        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        assert binary_sensor.is_on is True

    def test_storm_expected_next_hour_is_off(self, mock_coordinator: Mock) -> None:
        """Test storm_expected_1h sensor with no storm in forecast."""
        now = datetime.datetime.now(datetime.UTC)
        in_two_hours = now + datetime.timedelta(hours=2)

        # Setup coordinator data with storm forecast beyond 1 hour
        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": in_two_hours.isoformat(),
                    "condition": "lightning-rainy",
                    "temperature": 20,
                }
            ]
        }

        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        assert binary_sensor.is_on is False

    def test_storm_expected_invalid_datetime(self, mock_coordinator: Mock) -> None:
        """Test storm_expected_1h sensor with invalid datetime."""
        # Setup coordinator data with invalid datetime
        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": "invalid-datetime",
                    "condition": "lightning-rainy",
                    "temperature": 20,
                }
            ]
        }

        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        # Should handle invalid datetime gracefully
        assert binary_sensor.is_on is False

    def test_has_entity_name_property(self, mock_coordinator: Mock) -> None:
        """Test that has_entity_name property returns True."""
        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        assert binary_sensor.has_entity_name is True

    def test_storm_detection_timezone_naive_datetime(
        self, mock_coordinator: Mock
    ) -> None:
        """Test storm detection with timezone-naive datetime in forecast."""
        # Naive forecast times are Budapest local time
        now = datetime.datetime.now(TimeUtils.get_local_timezone())
        # Create a timezone-naive datetime string for 30 minutes from now
        future_time = now + timedelta(minutes=30)
        naive_datetime_str = future_time.replace(tzinfo=None).isoformat()

        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": naive_datetime_str,
                    "condition": "lightning-rainy",
                }
            ]
        }

        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        # Should detect storm even with timezone-naive datetime
        assert binary_sensor.is_on is True

    def test_storm_naive_datetime_not_treated_as_utc(
        self, mock_coordinator: Mock
    ) -> None:
        """Naive local times must not be shifted by the UTC offset."""
        local_tz = TimeUtils.get_local_timezone()
        # A slot that ended 30 minutes ago in local time
        past = datetime.datetime.now(local_tz) - timedelta(minutes=90)
        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": past.replace(tzinfo=None).isoformat(),
                    "condition": "lightning",
                }
            ]
        }
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=DESCRIPTIONS["storm_expected_1h"],
        )
        assert binary_sensor.is_on is False

    def test_storm_in_current_hour_slot_is_on(self, mock_coordinator: Mock) -> None:
        """A storm in the hour slot that has already started counts."""
        now = datetime.datetime.now(datetime.UTC)
        slot_start = now - timedelta(minutes=20)
        mock_coordinator.data = {
            "hourly_forecast": [
                {"datetime": slot_start.isoformat(), "condition": "lightning"}
            ]
        }
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=DESCRIPTIONS["storm_expected_1h"],
        )
        assert binary_sensor.is_on is True

    def test_storm_ignores_non_storm_conditions(self, mock_coordinator: Mock) -> None:
        """Non-storm conditions do not trigger the sensor."""
        now = datetime.datetime.now(datetime.UTC)
        mock_coordinator.data = {
            "hourly_forecast": [
                {
                    "datetime": (now + timedelta(minutes=30)).isoformat(),
                    "condition": "rainy",
                }
            ]
        }
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=DESCRIPTIONS["storm_expected_1h"],
        )
        assert binary_sensor.is_on is False
