"""Tests for the integration's local brand images (brand/ folder)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BRAND_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "idokep_weather_and_forecast"
    / "brand"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_TYPE_RGBA = 6


def read_png(path: Path) -> tuple[int, int, int, list[bytes]]:
    """Return width, height, colour type and raw scanlines of a PNG."""
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE
    pos = 8
    idat = b""
    width = height = color_type = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, _depth, color_type = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1  # RGBA, 8-bit, one filter byte per row
    rows = [raw[i * stride : (i + 1) * stride] for i in range(height)]
    return width, height, color_type, rows


def test_icon_is_256_square_rgba() -> None:
    """Home Assistant expects a 256x256 icon.png; RGBA allows transparency."""
    width, height, color_type, _ = read_png(BRAND_DIR / "icon.png")
    assert (width, height) == (256, 256)
    assert color_type == COLOR_TYPE_RGBA


def test_icon_background_is_transparent() -> None:
    """The top row is background only and must be fully transparent."""
    _, _, _, rows = read_png(BRAND_DIR / "icon.png")
    # For the first row every PNG filter encodes an all-zero alpha channel
    # as zeros, so the filtered bytes can be checked directly
    alphas = rows[0][1:][3::4]
    assert set(alphas) == {0}
