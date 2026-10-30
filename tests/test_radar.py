"""Tests for the national Radar service: GIF, frame fetching, entry, entity."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING, TypeVar
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from PIL import Image

from custom_components.idokep_weather_and_forecast import (
    RADAR_PLATFORMS,
    _ensure_radar_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.idokep_weather_and_forecast.api import (
    IdokepApiClientCommunicationError,
)
from custom_components.idokep_weather_and_forecast.config_flow import IdokepFlowHandler
from custom_components.idokep_weather_and_forecast.const import (
    DOMAIN,
    ENTRY_TYPE,
    RADAR,
    RADAR_TITLE,
    RADAR_UNIQUE_ID,
)
from custom_components.idokep_weather_and_forecast.image import IdokepRadarImage
from custom_components.idokep_weather_and_forecast.radar import (
    FRAME_COUNT,
    LAST_FRAME_MS,
    Frame,
    RadarCoordinator,
    RadarImage,
    build_gif,
    fetch_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

LM_1 = "Thu, 29 Oct 2026 23:37:36 GMT"
LM_2 = "Thu, 29 Oct 2026 23:42:36 GMT"


def jpeg(color: tuple[int, int, int]) -> bytes:
    """Return a small JPEG of one colour."""
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format="JPEG")
    return buffer.getvalue()


FRAMES = {i: jpeg((i * 40, 100, 200)) for i in range(1, 6)}


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------


def test_build_gif_is_a_looping_animation() -> None:
    """Frames become one looping GIF, with a pause on the newest."""
    frames = [FRAMES[i] for i in (5, 4, 3, 2, 1)]
    gif = Image.open(io.BytesIO(build_gif(frames)))

    assert gif.format == "GIF"
    assert gif.n_frames == len(frames)
    assert gif.info["loop"] == 0
    gif.seek(gif.n_frames - 1)
    assert gif.info["duration"] == LAST_FRAME_MS


def test_build_gif_unsqueezes_frames() -> None:
    """Squeezed 840×626 frames are widened to idokep.hu's 970:648 map aspect."""
    buffer = io.BytesIO()
    Image.new("RGB", (840, 626), (10, 20, 30)).save(buffer, format="JPEG")
    gif = Image.open(io.BytesIO(build_gif([buffer.getvalue()] * 2)))

    assert gif.size == (937, 626)


# ---------------------------------------------------------------------------
# Frame fetching
# ---------------------------------------------------------------------------


def response(status: int, body: bytes = b"", last_modified: str | None = None) -> Mock:
    """Return a mock aiohttp response usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.headers = {"Last-Modified": last_modified} if last_modified else {}
    resp.raise_for_status = Mock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_fetch_frame_sends_if_modified_since() -> None:
    """A changed frame is downloaded with its Last-Modified time."""
    session = Mock()
    session.get = Mock(return_value=response(200, b"jpg", LM_2))

    frame = await fetch_frame(session, 1, LM_1)

    assert frame.data == b"jpg"
    assert frame.last_modified == LM_2
    url = session.get.call_args.args[0]
    assert url == "https://www.idokep.hu/radar/radar1.jpg"
    assert session.get.call_args.kwargs["headers"]["If-Modified-Since"] == LM_1


@pytest.mark.asyncio
async def test_fetch_frame_unchanged_returns_none() -> None:
    """HTTP 304: nothing downloaded."""
    session = Mock()
    session.get = Mock(return_value=response(304))
    assert await fetch_frame(session, 1, LM_1) is None


@pytest.mark.asyncio
async def test_fetch_frame_error() -> None:
    """Network errors become IdokepApiClientCommunicationError."""
    import aiohttp

    session = Mock()
    session.get = Mock(side_effect=aiohttp.ClientError("down"))
    with pytest.raises(IdokepApiClientCommunicationError):
        await fetch_frame(session, 1)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class FakeSite:
    """Serves radar1..5 like idokep.hu: 304 if unchanged."""

    def __init__(self) -> None:
        """Start with five frames."""
        self.frames = dict(FRAMES)
        self.last_modified = LM_1
        self.requests: list[int] = []
        self.down = False

    def new_frame(self, data: bytes, last_modified: str | None) -> None:
        """Shift the frames by one, like the site does every ~5 minutes."""
        for i in range(5, 1, -1):
            self.frames[i] = self.frames[i - 1]
        self.frames[1] = data
        self.last_modified = last_modified

    async def fetch(
        self, _session: object, index: int, ims: str | None = None
    ) -> Frame | None:
        """Stand-in for radar.fetch_frame."""
        self.requests.append(index)
        if self.down:
            msg = "down"
            raise IdokepApiClientCommunicationError(msg)
        if ims == self.last_modified:
            return None
        return Frame(self.frames[index], self.last_modified)


@pytest.fixture
def site() -> FakeSite:
    """Return a fake idokep.hu radar."""
    return FakeSite()


@pytest.fixture
def coordinator(mock_hass: Mock) -> RadarCoordinator:
    """Return a radar coordinator (downloads go to the fake site)."""
    entry = Mock()
    entry.pref_disable_polling = False
    coordinator = RadarCoordinator(mock_hass, getLogger(__name__), entry, Mock())

    async def run_in_executor(func: Callable[..., T], *args: object) -> T:
        return func(*args)

    mock_hass.async_add_executor_job = run_in_executor
    return coordinator


async def update_at(coordinator: RadarCoordinator, now: datetime) -> RadarImage:
    """Run one update as if it were now; store the result like a refresh."""
    with patch.object(dt_util, "utcnow", return_value=now):
        coordinator.data = await coordinator._async_update_data()
    return coordinator.data


T0 = datetime(2026, 10, 29, 23, 38, tzinfo=UTC)


def gif_frames(gif: bytes) -> int:
    """Return the number of frames in a GIF."""
    return Image.open(io.BytesIO(gif)).n_frames


@pytest.fixture(autouse=True)
def fake_fetch(site: FakeSite):
    """Route frame downloads to the fake site."""
    with patch(
        "custom_components.idokep_weather_and_forecast.radar.fetch_frame", site.fetch
    ):
        yield


@pytest.mark.asyncio
async def test_first_update_downloads_all_frames(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """Start-up downloads radar1, then radar5..radar2."""
    image = await update_at(coordinator, T0)

    assert sorted(site.requests) == [1, 2, 3, 4, 5]
    assert gif_frames(image.gif) == FRAME_COUNT
    assert image.updated == datetime(2026, 10, 29, 23, 37, 36, tzinfo=UTC)
    # Oldest first: radar5 .. radar1
    assert list(coordinator._frames) == [FRAMES[i] for i in (5, 4, 3, 2, 1)]


@pytest.mark.asyncio
async def test_unchanged_frame_costs_one_cheap_request(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """Between new frames only a 304 check is made; the GIF is kept."""
    first = await update_at(coordinator, T0)
    site.requests.clear()

    second = await update_at(coordinator, T0 + timedelta(minutes=1))

    assert site.requests == [1]
    assert second is first


@pytest.mark.asyncio
async def test_new_frame_downloads_only_that_frame(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """A new frame shifts the others along without re-downloading them."""
    await update_at(coordinator, T0)
    site.requests.clear()
    new = jpeg((0, 0, 0))
    site.new_frame(new, LM_2)

    image = await update_at(coordinator, T0 + timedelta(minutes=5))

    assert site.requests == [1]
    assert list(coordinator._frames) == [FRAMES[i] for i in (4, 3, 2, 1)] + [new]
    assert image.updated == datetime(2026, 10, 29, 23, 42, 36, tzinfo=UTC)
    assert gif_frames(image.gif) == FRAME_COUNT


@pytest.mark.asyncio
async def test_next_frame_seen_late_still_shifts(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """Published 5 min after the previous, first seen 10 min later: one frame."""
    await update_at(coordinator, T0)
    site.requests.clear()
    site.new_frame(jpeg((0, 0, 0)), LM_2)  # published 23:42:36

    await update_at(coordinator, T0 + timedelta(minutes=10))

    assert site.requests == [1]


@pytest.mark.asyncio
async def test_missed_frames_still_only_shift(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """After missed frames only the newest is added; the gap is not refilled."""
    await update_at(coordinator, T0)
    site.requests.clear()
    missed = jpeg((0, 0, 0))
    site.new_frame(missed, LM_2)
    newest = jpeg((9, 9, 9))
    site.new_frame(newest, "Thu, 29 Oct 2026 23:47:36 GMT")

    await update_at(coordinator, T0 + timedelta(minutes=15))

    assert site.requests == [1]
    frames = list(coordinator._frames)
    assert frames == [FRAMES[i] for i in (4, 3, 2, 1)] + [newest]
    assert missed not in frames


@pytest.mark.asyncio
async def test_rewritten_same_picture_is_not_a_new_frame(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """A newer Last-Modified with the same picture doesn't add a frame."""
    await update_at(coordinator, T0)
    before = list(coordinator._frames)
    site.last_modified = LM_2  # same radar1 content

    await update_at(coordinator, T0 + timedelta(minutes=5))

    assert list(coordinator._frames) == before


@pytest.mark.asyncio
async def test_failure_keeps_last_animation(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """When idokep.hu is down the last GIF stays."""
    first = await update_at(coordinator, T0)
    site.down = True
    assert await update_at(coordinator, T0 + timedelta(minutes=1)) is first


@pytest.mark.asyncio
async def test_failure_without_animation_fails_update(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """With nothing to show, the update fails (setup is retried later)."""
    site.down = True
    with pytest.raises(UpdateFailed):
        await update_at(coordinator, T0)


@pytest.mark.asyncio
async def test_tick_skips_when_disabled_or_polling_off(
    coordinator: RadarCoordinator,
) -> None:
    """No checks while the entity is disabled or polling is turned off."""
    with patch.object(coordinator, "async_refresh", AsyncMock()) as refresh:
        await coordinator.async_tick(SLOT)  # nobody listening
        refresh.assert_not_awaited()

        coordinator.async_add_listener(lambda: None)
        coordinator.config_entry.pref_disable_polling = True
        await coordinator.async_tick(SLOT)
        refresh.assert_not_awaited()

        coordinator.config_entry.pref_disable_polling = False
        await coordinator.async_tick(SLOT)
        refresh.assert_awaited_once()


# Timer times: every minute at second 59; 23:40:59 is a 5-minute check
SLOT = datetime(2026, 10, 29, 23, 40, 59, tzinfo=UTC)


def minute(n: int) -> datetime:
    """Return the timer time n minutes after SLOT."""
    return SLOT + timedelta(minutes=n)


@pytest.mark.asyncio
async def test_checks_on_every_fifth_minute_only_after_a_frame(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """With the slot's frame found, only x:x0:59 / x:x5:59 check again."""
    coordinator.async_add_listener(lambda: None)
    await update_at(coordinator, T0)  # start-up: latest frame is here
    site.requests.clear()

    with patch.object(dt_util, "utcnow", return_value=SLOT):
        for n in range(1, 5):  # 23:41:59 .. 23:44:59
            await coordinator.async_tick(minute(n))
    assert site.requests == []

    site.new_frame(jpeg((0, 0, 0)), LM_2)
    with patch.object(dt_util, "utcnow", return_value=minute(5)):
        await coordinator.async_tick(minute(5))  # 23:45:59
    assert site.requests == [1]


@pytest.mark.asyncio
async def test_retries_every_minute_until_new_frame(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """No new frame at x:x0:59 yet: check each minute until it arrives."""
    coordinator.async_add_listener(lambda: None)
    await update_at(coordinator, T0)
    site.requests.clear()

    with patch.object(dt_util, "utcnow", return_value=SLOT):
        await coordinator.async_tick(SLOT)  # 23:40:59: unchanged (304)
        await coordinator.async_tick(minute(1))  # 23:41:59: still unchanged
    assert site.requests == [1, 1]

    site.new_frame(jpeg((0, 0, 0)), LM_2)
    with patch.object(dt_util, "utcnow", return_value=minute(2)):
        await coordinator.async_tick(minute(2))  # 23:42:59: new frame
        await coordinator.async_tick(minute(3))  # found: no more retries
        await coordinator.async_tick(minute(4))
    assert site.requests == [1, 1, 1]


@pytest.mark.asyncio
async def test_retries_every_minute_after_failure(
    coordinator: RadarCoordinator, site: FakeSite
) -> None:
    """A failed check is retried a minute later."""
    coordinator.async_add_listener(lambda: None)
    await update_at(coordinator, T0)
    site.requests.clear()
    site.down = True

    with patch.object(dt_util, "utcnow", return_value=SLOT):
        await coordinator.async_tick(SLOT)
        await coordinator.async_tick(minute(1))
    assert site.requests == [1, 1]


# ---------------------------------------------------------------------------
# Image entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_entity(coordinator: RadarCoordinator) -> None:
    """Names, device and the GIF as requested."""
    coordinator.config_entry.entry_id = "radar_entry"
    await update_at(coordinator, T0)
    # ImageEntity asks Home Assistant for its shared web client
    with patch("homeassistant.components.image.get_async_client"):
        entity = IdokepRadarImage(coordinator.hass, coordinator)

    assert entity.name == "National radar image"
    assert entity.content_type == "image/gif"
    assert entity.unique_id == "radar_entry_national_radar"
    assert entity.device_info["name"] == "National Radar Image"
    assert entity.device_info["model"] == "National radar image"
    assert entity.device_info["identifiers"] == {(DOMAIN, "radar_entry")}
    assert entity.image_last_updated == coordinator.data.updated
    assert await entity.async_image() == coordinator.data.gif


# ---------------------------------------------------------------------------
# Config flow: the Radar entry is created automatically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_step_creates_radar_entry() -> None:
    """The import step makes the Radar service entry without a form."""
    flow = IdokepFlowHandler()
    flow.hass = Mock()
    with (
        patch.object(flow, "async_set_unique_id", AsyncMock()) as set_id,
        patch.object(flow, "_abort_if_unique_id_configured"),
        patch.object(flow, "async_create_entry") as create,
    ):
        await flow.async_step_import()

    set_id.assert_awaited_once_with(RADAR_UNIQUE_ID)
    create.assert_called_once_with(title=RADAR_TITLE, data={ENTRY_TYPE: RADAR})


# ---------------------------------------------------------------------------
# Setup of the Radar entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_radar_entry_setup_and_unload() -> None:
    """The Radar entry sets up only the image platform and a minute timer."""
    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    entry = Mock()
    entry.data = {ENTRY_TYPE: RADAR}
    radar = AsyncMock()

    with (
        patch(
            "custom_components.idokep_weather_and_forecast.RadarCoordinator",
            return_value=radar,
        ),
        patch("custom_components.idokep_weather_and_forecast.async_get_clientsession"),
        patch(
            "custom_components.idokep_weather_and_forecast.async_track_time_change",
            return_value="unsub",
        ) as track,
        patch(
            "custom_components.idokep_weather_and_forecast.IdokepDataUpdateCoordinator"
        ) as location_coordinator,
    ):
        assert await async_setup_entry(hass, entry) is True

    location_coordinator.assert_not_called()
    radar.async_config_entry_first_refresh.assert_awaited_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, [Platform.IMAGE]
    )
    track.assert_called_once_with(hass, radar.async_tick, second=59)
    entry.async_on_unload.assert_called_once_with("unsub")

    assert await async_unload_entry(hass, entry) is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, RADAR_PLATFORMS
    )


# ---------------------------------------------------------------------------
# The Radar entry is (re)created when a location entry starts
# ---------------------------------------------------------------------------


def hass_with_entries(entries: list[Mock]) -> Mock:
    """Return a fake hass with these config entries."""
    hass = Mock()
    hass.config_entries.async_entries = Mock(return_value=entries)
    hass.async_create_task = Mock(side_effect=lambda coro: coro.close())
    return hass


def test_radar_entry_created_when_missing() -> None:
    """A location's startup (add, reload, restart) creates a missing Radar."""
    hass = hass_with_entries([Mock(unique_id="budapest")])
    _ensure_radar_entry(hass)
    hass.config_entries.async_entries.assert_called_once_with(DOMAIN)
    hass.config_entries.flow.async_init.assert_called_once_with(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT}
    )


def test_radar_entry_not_duplicated() -> None:
    """An existing Radar entry (also disabled or ignored) is left alone."""
    hass = hass_with_entries([Mock(unique_id=RADAR_UNIQUE_ID)])
    _ensure_radar_entry(hass)
    hass.config_entries.flow.async_init.assert_not_called()


@pytest.mark.asyncio
async def test_radar_entry_setup_does_not_create_itself() -> None:
    """Only location entries check for the Radar entry."""
    hass = hass_with_entries([])
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = Mock()
    entry.data = {ENTRY_TYPE: RADAR}
    with (
        patch(
            "custom_components.idokep_weather_and_forecast.RadarCoordinator",
            return_value=AsyncMock(),
        ),
        patch("custom_components.idokep_weather_and_forecast.async_get_clientsession"),
        patch("custom_components.idokep_weather_and_forecast.async_track_time_change"),
    ):
        await async_setup_entry(hass, entry)
    hass.config_entries.flow.async_init.assert_not_called()
