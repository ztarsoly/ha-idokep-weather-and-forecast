"""Custom types for idokep."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import IdokepApiClient
    from .coordinator import IdokepDataUpdateCoordinator
    from .radar import RadarCoordinator
    from .station import StationCoordinator


@dataclass
class IdokepData:
    """Data for the Idokep integration."""

    client: IdokepApiClient
    coordinator: IdokepDataUpdateCoordinator
    # The optional weather station's measurements (None without a station)
    station: StationCoordinator | None = None


@dataclass
class RadarData:
    """Data for the national Radar service entry."""

    coordinator: RadarCoordinator


# Type aliases for the location entries and the Radar entry
if TYPE_CHECKING:
    IdokepConfigEntry = ConfigEntry[IdokepData]
    RadarConfigEntry = ConfigEntry[RadarData]
