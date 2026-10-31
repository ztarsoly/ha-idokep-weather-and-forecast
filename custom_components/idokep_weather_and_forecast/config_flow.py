"""Adds config flow for Idokep."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from slugify import slugify

from .api import IdokepApiClient, IdokepApiClientError
from .const import (
    DOMAIN,
    ENTRY_TYPE,
    LOCATION,
    LOGGER,
    MERGE_METHOD,
    MERGE_METHODS,
    MERGE_PRIORITY,
    RADAR,
    RADAR_TITLE,
    RADAR_UNIQUE_ID,
    STATION,
)
from .station import (
    StationDataError,
    fetch_station,
    merge_method,
    parse_codes,
    station_codes,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class WeatherDataFetchError(ValueError):
    """Exception raised when weather data cannot be fetched for a given location."""

    def __init__(self, location: str) -> None:
        """Initialize WeatherDataFetchError with the given location."""
        msg = f"Could not fetch weather data for {location}"
        super().__init__(msg)


def _name_field(default: str | None) -> dict[vol.Required, Any]:
    """Return the entry name field of a form."""
    return {
        vol.Required(
            CONF_NAME,
            default=default or vol.UNDEFINED,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT),
        ),
    }


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


def _station_fields(codes: list[str], method: str) -> dict[vol.Optional, Any]:
    """Return the optional station codes and merge method fields of a form."""
    return {
        vol.Optional(
            STATION,
            description={"suggested_value": codes} if codes else None,
        ): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT, multiple=True
            ),
        ),
        vol.Optional(MERGE_METHOD, default=method): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=MERGE_METHODS,
                translation_key=MERGE_METHOD,
                mode=selector.SelectSelectorMode.DROPDOWN,
            ),
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


async def _async_station_error(
    hass: HomeAssistant, codes: list[str]
) -> tuple[str, str] | None:
    """Return (form error, station code) for the first unusable code, or None."""
    session = async_create_clientsession(hass)
    for code in codes:
        try:
            await fetch_station(session, code)
        except StationDataError:
            return "station", code
        except IdokepApiClientError as exception:
            LOGGER.error(exception)
            return "station_connection", code
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
        placeholders = {"code": ""}
        codes: list[str] = []
        method = MERGE_PRIORITY
        if user_input is not None:
            user_input = {**user_input}
            # The name is the entry's title (and so the device names), not data
            name = (user_input.pop(CONF_NAME, "") or "").strip()
            codes = parse_codes(user_input.pop(STATION, None))
            method = user_input.pop(MERGE_METHOD, MERGE_PRIORITY)
            if not name:
                _errors[CONF_NAME] = "name"
            elif error := await _async_location_error(self.hass, user_input[LOCATION]):
                _errors["base"] = error
            elif problem := await _async_station_error(self.hass, codes):
                _errors[STATION], placeholders["code"] = problem
            if not _errors:
                await self.async_set_unique_id(unique_id=slugify(user_input[LOCATION]))
                self._abort_if_unique_id_configured()
                if codes:
                    user_input[STATION] = codes
                user_input[MERGE_METHOD] = method
                return self.async_create_entry(title=name, data=user_input)
            user_input[CONF_NAME] = name

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    **_name_field((user_input or {}).get(CONF_NAME)),
                    **_location_field((user_input or {}).get(LOCATION)),
                    **_station_fields(codes, method),
                },
            ),
            errors=_errors,
            description_placeholders=placeholders,
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
    """Change a location entry's name, location and weather station."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the name, the location, the station codes and the merge method."""
        errors: dict[str, str] = {}
        placeholders = {"code": ""}
        name = self._entry.title
        location = self._entry.data[LOCATION]
        codes = station_codes(self._entry)
        method = merge_method(self._entry)
        if user_input is not None:
            name = (user_input.get(CONF_NAME) or "").strip()
            location = user_input[LOCATION].strip()
            codes = parse_codes(user_input.get(STATION))
            method = user_input.get(MERGE_METHOD, MERGE_PRIORITY)
            if not name:
                errors[CONF_NAME] = "name"
            if location != self._entry.data[LOCATION]:
                errors.update(await self._async_location_errors(location))
            if not errors and (
                problem := await _async_station_error(self.hass, codes)
            ):
                errors[STATION], placeholders["code"] = problem
            if not errors:
                options = {STATION: codes, MERGE_METHOD: method}
                self._async_update_entry(name, location, options)
                # Reloaded afterwards, which adds or removes the station device
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_name_field(name),
                    **_location_field(location),
                    **_station_fields(codes, method),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
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

    def _async_update_entry(
        self, name: str, location: str, options: dict[str, Any]
    ) -> None:
        """Apply a new name and location (the station is saved as an option)."""
        entry = self._entry
        changes: dict[str, Any] = {}
        if name != entry.title:
            self._async_rename_devices(entry.title, name)
            changes["title"] = name
        if location != entry.data[LOCATION]:
            changes["data"] = {**entry.data, LOCATION: location}
            changes["unique_id"] = slugify(location)
        if not changes:
            return
        self.hass.config_entries.async_update_entry(entry, **changes)
        if options == dict(entry.options):
            # Options unchanged, so the automatic reload won't happen
            self.hass.config_entries.async_schedule_reload(entry.entry_id)

    def _async_rename_devices(self, old: str, new: str) -> None:
        """
        Put the new entry name into the entry's device names.

        Covers "<name> Forecast" and "<name> Weather", also when renamed by
        the user, as long as the name still contains the old entry name.
        """
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self._entry.entry_id
        ):
            changes = {
                field: value.replace(old, new)
                for field in ("name", "name_by_user")
                if (value := getattr(device, field)) and old in value
            }
            if changes:
                registry.async_update_device(device.id, **changes)
