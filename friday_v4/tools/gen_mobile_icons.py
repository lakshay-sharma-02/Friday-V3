#!/usr/bin/env python3
"""Generate the mobile companion PWA icons — pure stdlib, no PIL.

Writes ``icon-192.png``, ``icon-512.png`` and ``apple-touch-icon.png``
(180px) into ``src/friday_v4/mobile/app/``. The glyph is the FRIDAY
mark: a cyan diamond on the dark navy panel, with a soft rounded
squircle mask (maskable-safe — the diamond sits well inside the safe
zone).

PNG is written by hand (zlib-compressed RGBA scanlines + CRC-chunked
IHDR/IDAT/IEND) so the repo never needs Pillow or any raster library —
consistent with the project's pure-stdlib-first law.

Usage:  python tools/gen_mobile_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

_OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "friday_v4" / "mobile" / "app"

# ── palette ──────────────────────────────────────────────────────────
BG = (10, 14, 23, 255)        # #0a0e17 — panel navy
CYAN = (56, 212, 245, 255)    # #38d4f5 — the FRIDAY accent
CYAN_DIM = (31, 106, 124, 255)  # translucent deep for the glow ring

#: Diamond half-diagonals as a fraction of the canvas (kept inside the
#: maskable safe zone of ~80%).
_DIAMOND_FRAC = 0.34
_RING_FRAC = 0.42


def _rounded_squircle(size: int, radius_frac: float = 0.22) -> list[list[float]]:
    """Per-pixel corner radius: returns a smooth 0..1 alpha for the mask."""
    radius = size * radius_frac
    cx = cy = (size - 1) / 2
    r = size / 2
    mask = []
    for y in range(size):
        row = []
        for x in range(size):
            # Distance from the rounded-square boundary (0 inside → 1 outside).
            dx = max(abs(x - cx) + 0.5 - (r - radius), 0.0)
            dy = max(abs(y - cy) + 0.5 - (r - radius), 0.0)
            d = ((dx * dx + dy * dy) ** 0.5) / radius
            row.append(min(max(d, 0.0), 1.0))
        mask.append(row)
    return mask


def _in_diamond(size: int, x: int, y: int, half: float) -> bool:
    """Whether (x, y) lies inside the axis-aligned diamond of half-diagonal ``half``."""
    cx = cy = (size - 1) / 2
    return abs(x - cx) / half + abs(y - cy) / half <= 1.0


def _in_ring(size: int, x: int, y: int) -> float:
    """0 inside the glow ring band → 1 outside; the diamond sits inside."""
    cx = cy = (size - 1) / 2
    dx, dy = (x - cx) / (size * _RING_FRAC), (y - cy) / (size * _RING_FRAC)
    return min(max((dx * dx + dy * dy) ** 0.5, 0.0), 1.0)


def render_icon(size: int) -> bytes:
    """A size×size RGBA PNG of the FRIDAY mark."""
    half = size * _DIAMOND_FRAC
    corner = _rounded_squircle(size)
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter type 0 (None)
        for x in range(size):
            edge = corner[y][x]  # 0 inside squircle → 1 at corners
            if edge >= 1.0:
                row += bytes((0, 0, 0, 0))  # fully outside → transparent
                continue
            alpha = 1.0 - edge
            if _in_diamond(size, x, y, half):
                # Cyan diamond body with a subtle horizontal sheen.
                sheen = 1.0 - 0.25 * (y / size)
                r = int(CYAN[0] * sheen)
                g = int(CYAN[1] * sheen)
                b = int(CYAN[2] * sheen)
            elif _in_ring(size, x, y) < 1.0:
                # Faint cyan glow ring around the diamond.
                t = 1.0 - _in_ring(size, x, y)
                r = int(CYAN_DIM[0] * t + BG[0] * (1 - t))
                g = int(CYAN_DIM[1] * t + BG[1] * (1 - t))
                b = int(CYAN_DIM[2] * t + BG[2] * (1 - t))
            else:
                r, g, b, _ = BG
            row += bytes((r, g, b, int(255 * alpha)))
        rows.append(bytes(row))

    raw = b"".join(rows)
    compressed = zlib.compress(raw, 9)

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + ctype + data
                + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b""))


def main() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),  # iOS add-to-home-screen
    ]
    for name, size in specs:
        out = _OUT_DIR / name
        out.write_bytes(render_icon(size))
        print(f"  wrote {out.relative_to(_OUT_DIR.parent.parent.parent)} "
              f"({size}×{size}, {out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
