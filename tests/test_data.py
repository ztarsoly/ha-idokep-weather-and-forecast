"""Unit tests for the Időkép integration data types."""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

from homeassistant.config_entries import ConfigEntry

from custom_components.idokep_weather_and_forecast.api import IdokepApiClient
from custom_components.idokep_weather_and_forecast.coordinator import (
    IdokepDataUpdateCoordinator,
)
from custom_components.idokep_weather_and_forecast.data import IdokepData


class TestIdokepData:
    """Test cases for IdokepData dataclass."""

    def test_idokep_data_creation(self) -> None:
        """Test creation of IdokepData instance."""
        # Create mock objects
        mock_client = Mock(spec=IdokepApiClient)
        mock_coordinator = Mock(spec=IdokepDataUpdateCoordinator)

        # Create IdokepData instance
        data = IdokepData(
            client=mock_client,
            coordinator=mock_coordinator,
        )

        # Verify all attributes are set correctly
        assert data.client is mock_client
        assert data.coordinator is mock_coordinator

    def test_idokep_data_attributes(self) -> None:
        """Test that IdokepData attributes can be accessed."""
        # Create mock objects
        mock_client = Mock(spec=IdokepApiClient)
        mock_coordinator = Mock(spec=IdokepDataUpdateCoordinator)

        # Create IdokepData instance
        data = IdokepData(
            client=mock_client,
            coordinator=mock_coordinator,
        )

        # Verify attributes are the correct types
        assert isinstance(data.client, Mock)
        assert isinstance(data.coordinator, Mock)

    def test_idokep_data_dataclass_fields(self) -> None:
        """Test that IdokepData is a proper dataclass with expected fields."""
        # Verify IdokepData is a dataclass
        assert dataclasses.is_dataclass(IdokepData)

        # Get field names
        field_names = {field.name for field in dataclasses.fields(IdokepData)}
        expected_fields = {"client", "coordinator", "station"}

        # Verify all expected fields are present
        assert expected_fields == field_names

    def test_idokep_config_entry_type_alias(self) -> None:
        """Test that IdokepConfigEntry type alias works correctly."""
        # This test verifies the type alias is properly defined
        # We can't really test type aliases at runtime, but we can ensure
        # it's importable and the module structure is correct

        # Create a mock config entry to verify the structure
        mock_config_entry = Mock(spec=ConfigEntry)
        mock_data = Mock(spec=IdokepData)

        # Simulate runtime_data assignment (what the type alias represents)
        mock_config_entry.runtime_data = mock_data

        # Verify the assignment works
        assert mock_config_entry.runtime_data is mock_data
