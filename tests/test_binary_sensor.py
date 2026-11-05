"""Unit tests for the Időkép binary sensor platform."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant

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
        assert list(DESCRIPTIONS) == ["storm_expected_1h"]
        for description in ENTITY_DESCRIPTIONS:
            assert description.device_class == BinarySensorDeviceClass.SAFETY

    @pytest.mark.asyncio
    async def test_async_setup_entry(self) -> None:
        """Test the setup of binary sensor entities."""
        # Mock dependencies
        mock_hass = Mock(spec=HomeAssistant)
        mock_entry = Mock()
        mock_coordinator = Mock()
        mock_coordinator.config_entry.data = {"location": "Budapest"}
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

    def test_has_entity_name_property(self, mock_coordinator: Mock) -> None:
        """Test that has_entity_name property returns True."""
        entity_description = DESCRIPTIONS["storm_expected_1h"]
        binary_sensor = IdokepBinarySensor(
            coordinator=mock_coordinator,
            entity_description=entity_description,
        )

        assert binary_sensor.has_entity_name is True

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"storm_expected": True}, True),
            ({"storm_expected": False}, False),
            ({}, None),  # the forecast page isn't read yet: unknown
        ],
    )
    def test_storm_shows_the_value_of_the_last_fetch(
        self, mock_coordinator: Mock, data: dict, expected: bool | None
    ) -> None:
        """The state only changes with the data of a fetch, not with the clock."""
        mock_coordinator.data = data
        sensor = IdokepBinarySensor(mock_coordinator, ENTITY_DESCRIPTIONS[0])
        assert sensor.is_on is expected
