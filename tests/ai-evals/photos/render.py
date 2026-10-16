"""Synthetic reefer-panel fixtures. No photographs, names, or real phone numbers.

Each PNG is a flat panel whose color stands in for the variation in labels.json.
A later vision eval reads the label, not text baked into the pixels.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

_COLORS = {
    "clear": (40, 180, 90),
    "angle_15": (40, 140, 180),
    "angle_10": (40, 140, 180),
    "angle_25": (180, 140, 40),
    "angle_35": (180, 100, 40),
    "angle_20": (180, 120, 40),
    "glare_low": (220, 220, 120),
    "glare_high": (240, 240, 240),
    "blur": (70, 70, 80),
    "low_light": (20, 20, 30),
    "motion_blur": (50, 50, 70),
    "wrong_unit": (160, 60, 60),
}


def render_photos(directory: Path | None = None) -> list[Path]:
    """Write one PNG per label beside labels.json."""

    folder = directory or Path(__file__).resolve().parent
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    written: list[Path] = []
    for label in labels:
        color = _COLORS[label["variation"][0]]
        path = folder / label["file"]
        path.write_bytes(_png(32, 16, color))
        written.append(path)
    return written


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", checksum)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    payload += chunk(b"IDAT", zlib.compress(raw))
    return payload + chunk(b"IEND", b"")


if __name__ == "__main__":
    render_photos()
