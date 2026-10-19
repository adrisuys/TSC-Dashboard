#!/usr/bin/env python3
"""Regenerate the PWA icons in `static/icons/`.

    python3 webapp/make_icons.py

Pure stdlib — the web app's venv has no image library, and the icons are a
handful of flat shapes, so they are rasterised here from signed distance
fields (one sample per pixel, the distance gives the antialiased edge).

The mark is a bold "D" on the app's accent blue (`--link`, #1a56db). Three
shapes come out of it:

- `icon-<n>.png`      the rounded square, transparent corners — `purpose: any`
- `icon-maskable.png` full bleed, glyph inside the 80% safe zone — `maskable`
- `apple-touch-icon.png`  full bleed square, no transparency: iOS rounds it

Run this again after changing BLUE or the proportions, then hard-reload the
app (an installed PWA caches its icons until it is reinstalled).
"""
import math
import os
import struct
import zlib

BLUE = (0x1A, 0x56, 0xDB)
WHITE = (0xFF, 0xFF, 0xFF)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "static", "icons")


def _sd_round_box(px, py, bx, by, r_tr, r_br, r_tl, r_bl):
    """Signed distance to a box with a radius per corner, y pointing up.

    Negative inside. The usual branchless formulation: pick the radius of the
    quadrant the point is in, shrink the box by it, then measure."""
    r_top, r_bot = (r_tr, r_br) if px > 0 else (r_tl, r_bl)
    r = r_top if py > 0 else r_bot
    qx = abs(px) - bx + r
    qy = abs(py) - by + r
    return min(max(qx, qy), 0.0) + math.hypot(max(qx, 0.0), max(qy, 0.0)) - r


def _d_glyph(px, py, s):
    """Distance to a capital D centred on the origin, sized against `s`.

    The outer silhouette is a box whose right corners are fully round and
    whose left ones are barely; the counter is the same trick inset by the
    stroke and pushed right by the stem. D = outer minus counter."""
    bx, by = 0.250 * s, 0.290 * s
    stem, stroke = 0.145 * s, 0.135 * s
    outer = _sd_round_box(px, py, bx, by, bx, bx, 0.035 * s, 0.035 * s)

    left, right = -bx + stem, bx - stroke
    cx, cbx = (left + right) / 2, (right - left) / 2
    cby = by - stroke
    counter = _sd_round_box(px - cx, py, cbx, cby, cbx, cbx, 0.0, 0.0)
    return max(outer, -counter)


def _coverage(d):
    """Distance (in pixels) to alpha: the edge spans one pixel."""
    return min(max(0.5 - d, 0.0), 1.0)


def _render(size, corner, glyph_scale, opaque):
    """One icon as RGBA rows.

    `corner` is the background's corner radius as a fraction of the size (0 =
    full-bleed square). `glyph_scale` shrinks the D — maskable icons must keep
    it inside the safe zone. `opaque` fills the corners with blue instead of
    leaving them transparent (Apple rejects transparency)."""
    half = size / 2.0
    r = corner * size
    rows = []
    for y in range(size):
        row = bytearray()
        # y up, and sample the pixel's centre.
        py = half - (y + 0.5)
        for x in range(size):
            px = (x + 0.5) - half
            bg = 1.0 if opaque else _coverage(
                _sd_round_box(px, py, half, half, r, r, r, r))
            fg = _coverage(_d_glyph(px, py, size * glyph_scale))
            # The white glyph over the blue plate, over transparency.
            alpha = bg + fg * (1 - bg)
            if alpha <= 0:
                row += b"\0\0\0\0"
                continue
            for i in range(3):
                # Premultiplied compositing, then back to straight alpha.
                c = (BLUE[i] * bg * (1 - fg) + WHITE[i] * fg) / alpha
                row.append(int(min(max(c, 0), 255) + 0.5))
            row.append(int(alpha * 255 + 0.5))
        rows.append(bytes(row))
    return rows


def _png(path, size, rows):
    """Write 8-bit RGBA rows as a PNG (filter 0 on every scanline)."""
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


# name -> (size, corner radius, glyph scale, opaque)
ICONS = {
    "icon-32.png": (32, 0.225, 1.0, False),
    "icon-180.png": (180, 0.225, 1.0, False),
    "icon-192.png": (192, 0.225, 1.0, False),
    "icon-512.png": (512, 0.225, 1.0, False),
    "icon-maskable-192.png": (192, 0.0, 0.8, True),
    "icon-maskable-512.png": (512, 0.0, 0.8, True),
    "apple-touch-icon.png": (180, 0.0, 1.0, True),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (size, corner, scale, opaque) in ICONS.items():
        path = os.path.join(OUT, name)
        _png(path, size, _render(size, corner, scale, opaque))
        print(f"✓ {name}  ({os.path.getsize(path):,} bytes)")


if __name__ == "__main__":
    main()
