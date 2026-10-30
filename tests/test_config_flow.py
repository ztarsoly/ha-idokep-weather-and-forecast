"""Tests for the Configure (options) dialog of a location entry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType

from custom_components.idokep_weather_and_forecast import config_flow
from custom_components.idokep_weather_and_forecast.config_flow import (
    IdokepFlowHandler,
    IdokepOptionsFlow,
)


def _flow(
    title: str = "Debrecen-Józsa",
    options: dict | None = None,
    others: list[str] | None = None,
) -> tuple[IdokepOptionsFlow, Mock]:
    """Return an options flow for a Debrecen-Józsa entry (and the entry)."""
    entry = Mock(
        entry_id="entry1",
        title=title,
        data={"location": "Debrecen-Józsa", "station": "jozsamet"},
        options=options if options is not None else {},
    )
    flow = IdokepOptionsFlow(entry)
    flow.hass = Mock()
    other_entries = [
        Mock(entry_id=f"other{i}", unique_id=unique_id)
        for i, unique_id in enumerate(others or [])
    ]
    flow.hass.config_entries.async_entries = Mock(
        return_value=[
            Mock(entry_id="entry1", unique_id="debrecen-jozsa"),
            *other_entries,
        ]
    )
    return flow, entry


def _patch_checks(
    location_error: str | None = None, station_error: str | None = None
) -> tuple[Any, Any]:
    """Patch the idokep.hu checks of the location and station code."""
    return (
        patch.object(
            config_flow,
            "_async_location_error",
            AsyncMock(return_value=location_error),
        ),
        patch.object(
            config_flow, "_async_station_error", AsyncMock(return_value=station_error)
        ),
    )


@pytest.mark.asyncio
async def test_form_shows_current_location_and_station() -> None:
    """The dialog has the location and the station code, filled in."""
    flow, _ = _flow()
    result = await flow.async_step_init()

    assert result["type"] == FlowResultType.FORM
    schema = {str(key): key for key in result["data_schema"].schema}
    assert list(schema) == ["location", "station"]
    assert schema["location"].default() == "Debrecen-Józsa"
    assert schema["station"].description == {"suggested_value": "jozsamet"}


@pytest.mark.asyncio
async def test_same_location_only_saves_station() -> None:
    """With the location unchanged, only the station option is saved."""
    flow, _ = _flow()
    location_check, station_check = _patch_checks()
    with location_check as check_location, station_check:
        result = await flow.async_step_init(
            {"location": "Debrecen-Józsa", "station": "ha0kaistvan"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"station": "ha0kaistvan"}
    check_location.assert_not_awaited()
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_new_location_updates_entry() -> None:
    """A new settlement changes the entry's location and unique ID, not its name."""
    flow, entry = _flow(options={"station": "jozsamet"})
    location_check, station_check = _patch_checks()
    with location_check, station_check:
        result = await flow.async_step_init(
            {"location": " Debrecen ", "station": "jozsamet"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # No title: the entry's name (and so the device names) stays
    flow.hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={"location": "Debrecen", "station": "jozsamet"},
        unique_id="debrecen",
    )
    # Options are unchanged, so the flow reloads the entry itself
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry1")


@pytest.mark.asyncio
async def test_new_location_with_new_station_reloads_once() -> None:
    """Options changed too: Home Assistant's automatic reload is enough."""
    flow, _ = _flow(options={"station": "jozsamet"})
    location_check, station_check = _patch_checks()
    with location_check, station_check:
        await flow.async_step_init({"location": "Debrecen", "station": "ha0kaistvan"})

    flow.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_location_of_another_entry_is_refused() -> None:
    """A location already set up in another entry is refused."""
    flow, _ = _flow(others=["budapest"])
    location_check, station_check = _patch_checks()
    with location_check, station_check:
        result = await flow.async_step_init(
            {"location": "Budapest", "station": "jozsamet"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"location": "already_configured"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_location_is_refused() -> None:
    """A location idokep.hu has no data for shows the connection error."""
    flow, _ = _flow()
    location_check, station_check = _patch_checks(location_error="connection")
    with location_check, station_check:
        result = await flow.async_step_init({"location": "Nowhere", "station": ""})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "connection"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_bad_station_does_not_move_location() -> None:
    """With a bad station code nothing is changed yet."""
    flow, _ = _flow()
    location_check, station_check = _patch_checks(station_error="station")
    with location_check, station_check:
        result = await flow.async_step_init({"location": "Debrecen", "station": "x"})

    assert result["errors"] == {"station": "station"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


# ---- new entry ---------------------------------------------------------------


def _user_flow() -> IdokepFlowHandler:
    flow = IdokepFlowHandler()
    flow.hass = Mock()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    return flow


@pytest.mark.asyncio
async def test_user_form_fields() -> None:
    """The new-entry form asks for the name, the settlement and the station."""
    result = await _user_flow().async_step_user()

    schema = {str(key): key for key in result["data_schema"].schema}
    assert list(schema) == ["name", "location", "station"]
    assert isinstance(schema["name"], vol.Required)
    assert isinstance(schema["location"], vol.Required)
    assert isinstance(schema["station"], vol.Optional)


@pytest.mark.asyncio
async def test_user_name_is_the_entry_title() -> None:
    """The name becomes the entry's title; only settlement and station are data."""
    flow = _user_flow()
    location_check, station_check = _patch_checks()
    with location_check, station_check:
        result = await flow.async_step_user(
            {"name": " Home ", "location": "Debrecen-Józsa", "station": "jozsamet"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {"location": "Debrecen-Józsa", "station": "jozsamet"}
    flow.async_set_unique_id.assert_awaited_once_with(unique_id="debrecen-jozsa")


@pytest.mark.asyncio
async def test_user_blank_name_is_refused() -> None:
    """A name of only spaces is refused, the other fields are kept."""
    location_check, station_check = _patch_checks()
    with location_check, station_check:
        result = await _user_flow().async_step_user(
            {"name": "  ", "location": "Debrecen-Józsa", "station": "jozsamet"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"name": "name"}
    schema = {str(key): key for key in result["data_schema"].schema}
    assert schema["location"].default() == "Debrecen-Józsa"
