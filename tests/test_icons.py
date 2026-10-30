"""Tests for the Időkép icon translations (icons.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.idokep_weather_and_forecast import binary_sensor, sensor
from custom_components.idokep_weather_and_forecast.api import WeatherConditionMapper

ICONS_FILE = (
    Path(__file__).parent.parent
    / "custom_components"
    / "idokep_weather_and_forecast"
    / "icons.json"
)


@pytest.fixture(scope="module")
def icons() -> dict:
    """Return the parsed icons.json entity section."""
    return json.loads(ICONS_FILE.read_text(encoding="utf-8"))["entity"]


@pytest.mark.parametrize("platform", [sensor, binary_sensor])
def test_descriptions_do_not_set_fixed_icons(platform: object) -> None:
    """A fixed icon on a description would override icons.json."""
    for description in platform.ENTITY_DESCRIPTIONS:
        assert description.icon is None, description.key


@pytest.mark.parametrize(
    ("domain", "platform"), [("sensor", sensor), ("binary_sensor", binary_sensor)]
)
def test_icon_keys_match_translation_keys(
    icons: dict, domain: str, platform: object
) -> None:
    """Every icons.json entry belongs to an existing entity."""
    descriptions = (
        *platform.ENTITY_DESCRIPTIONS,
        *getattr(platform, "STATION_DESCRIPTIONS", ()),
        *(
            [platform.STATION_MEASUREMENT_TIME_DESCRIPTION]
            if platform is sensor
            else []
        ),
    )
    translation_keys = {d.translation_key for d in descriptions}
    assert set(icons[domain]) <= translation_keys


def test_all_icons_use_mdi(icons: dict) -> None:
    """All icons are Material Design Icons."""
    for entries in icons.values():
        for entry in entries.values():
            assert entry["default"].startswith("mdi:")
            for icon in entry.get("state", {}).values():
                assert icon.startswith("mdi:")


def test_every_condition_has_an_icon(icons: dict) -> None:
    """Each condition the mapper can produce has its own icon."""
    conditions = set(WeatherConditionMapper._CONDITION_MAPPING.values())
    assert conditions <= set(icons["sensor"]["condition"]["state"])


def test_binary_sensors_differ_between_on_and_off(icons: dict) -> None:
    """The on icon differs from the off (default) icon."""
    for key, entry in icons["binary_sensor"].items():
        assert entry["state"]["on"] != entry["default"], key


def test_alert_levels_have_distinct_icons(icons: dict) -> None:
    """Yellow, orange and red alerts are told apart by icon."""
    on_icons = [
        icons["binary_sensor"][f"alert_{level}"]["state"]["on"]
        for level in ("yellow", "orange", "red")
    ]
    assert len(set(on_icons)) == len(on_icons)
