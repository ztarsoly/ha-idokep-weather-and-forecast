"""Adds config flow for Idokep."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from slugify import slugify

from .api import IdokepApiClient, IdokepApiClientError
from .const import (
    DOMAIN,
    ENTRY_TYPE,
    LOCATION,
    LOGGER,
    RADAR,
    RADAR_TITLE,
    RADAR_UNIQUE_ID,
    STATION,
)
from .station import StationDataError, fetch_station, station_code

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class WeatherDataFetchError(ValueError):
    """Exception raised when weather data cannot be fetched for a given location."""

    def __init__(self, location: str) -> None:
        """Initialize WeatherDataFetchError with the given location."""
        msg = f"Could not fetch weather data for {location}"
        super().__init__(msg)


def _location_field(default: str | None) -> dict[vol.Required, Any]:
    """Return the location name field of a form."""
    return {
        vol.Required(
            LOCATION,
            default=default or vol.UNDEFINED,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT),
        ),
    }


def _station_field(default: str | None) -> dict[vol.Optional, Any]:
    """Return the optional station code field of a form."""
    return {
        vol.Optional(
            STATION,
            description={"suggested_value": default} if default else None,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT),
        ),
    }


async def _async_location_error(hass: HomeAssistant, location: str) -> str | None:
    """Return the form error for a location name, or None if idokep.hu knows it."""
    client = IdokepApiClient(session=async_create_clientsession(hass))
    try:
        # Only the current conditions page is needed to validate the location
        data = await client.async_get_current_weather(location)
        if "temperature" not in data:
            raise WeatherDataFetchError(location)
    except (ValueError, IdokepApiClientError) as exception:
        LOGGER.error(exception)
        return "connection"
    except Exception:
        LOGGER.exception("Unexpected error validating location")
        return "unknown"
    return None


async def _async_station_error(hass: HomeAssistant, code: str | None) -> str | None:
    """Return the form error for a station code, or None if usable (or empty)."""
    if not code:
        return None
    try:
        await fetch_station(async_create_clientsession(hass), code)
    except StationDataError:
        return "station"
    except IdokepApiClientError as exception:
        LOGGER.error(exception)
        return "station_connection"
    return None


class IdokepFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Idokep."""

    VERSION = 1

    def is_matching(self, _other_flow: config_entries.ConfigFlow) -> bool:
        """Return True if config flows match."""
        return False

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow (station code of a location entry)."""
        return IdokepOptionsFlow(config_entry)

    @classmethod
    def async_supports_options_flow(
        cls, config_entry: config_entries.ConfigEntry
    ) -> bool:
        """Only location entries have options (not the Radar entry)."""
        return config_entry.data.get(ENTRY_TYPE) != RADAR

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            user_input = {**user_input}
            # The name is the entry's title (and so the device names), not data
            name = (user_input.pop(CONF_NAME, "") or "").strip()
            code = (user_input.pop(STATION, "") or "").strip()
            if not name:
                _errors[CONF_NAME] = "name"
            elif error := await _async_location_error(self.hass, user_input[LOCATION]):
                _errors["base"] = error
            elif error := await _async_station_error(self.hass, code):
                _errors[STATION] = error
            if not _errors:
                await self.async_set_unique_id(unique_id=slugify(user_input[LOCATION]))
                self._abort_if_unique_id_configured()
                if code:
                    user_input[STATION] = code
                return self.async_create_entry(title=name, data=user_input)
            user_input[CONF_NAME] = name
            if code:
                user_input[STATION] = code

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME,
                        default=(user_input or {}).get(CONF_NAME) or vol.UNDEFINED,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                    **_location_field((user_input or {}).get(LOCATION)),
                    **_station_field((user_input or {}).get(STATION)),
                },
            ),
            errors=_errors,
        )

    async def async_step_import(
        self, _import_data: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """
        Create the Radar service entry (no form).

        Started by a location entry's setup when no Radar entry exists.
        """
        await self.async_set_unique_id(RADAR_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=RADAR_TITLE, data={ENTRY_TYPE: RADAR})


class IdokepOptionsFlow(config_entries.OptionsFlowWithReload):
    """Change a location entry's location and weather station."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the location and the station code (empty removes the station)."""
        errors: dict[str, str] = {}
        location = self._entry.data[LOCATION]
        code = station_code(self._entry)
        if user_input is not None:
            location = user_input[LOCATION].strip()
            code = (user_input.get(STATION) or "").strip() or None
            if location != self._entry.data[LOCATION]:
                errors.update(await self._async_location_errors(location))
            if not errors and (error := await _async_station_error(self.hass, code)):
                errors[STATION] = error
            if not errors:
                options = {STATION: code or ""}
                if location != self._entry.data[LOCATION]:
                    self._async_move_to(location, options)
                # Reloaded afterwards, which adds or removes the station device
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {**_location_field(location), **_station_field(code)}
            ),
            errors=errors,
        )

    async def _async_location_errors(self, location: str) -> dict[str, str]:
        """Return the form errors for a new location name."""
        unique_id = slugify(location)
        if any(
            entry.unique_id == unique_id and entry.entry_id != self._entry.entry_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return {LOCATION: "already_configured"}
        if error := await _async_location_error(self.hass, location):
            return {"base": error}
        return {}

    def _async_move_to(self, location: str, options: dict[str, str]) -> None:
        """Switch the entry to another location (its name stays the same)."""
        entry = self._entry
        # The title (the name given in the config flow) is kept, and so are
        # the device names "<name> Forecast" and "<name> Weather"
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, LOCATION: location},
            unique_id=slugify(location),
        )
        if options == dict(entry.options):
            # Options unchanged, so the automatic reload won't happen
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
