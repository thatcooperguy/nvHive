#!/usr/bin/env python3
"""Render the placeholder mascot sprite sheet for the nvHive WebUI.

Stdlib only (zlib + struct write the PNG) so it runs on any Python 3 without
Pillow. The output is committed; the web build never runs this script.

    python web/public/mascot/generate_sheet.py            # writes sheet.png
    python web/public/mascot/generate_sheet.py --out x.png

Sheet layout (must match manifest.json):
    * 64x64 frames, 4 columns, one row per state
    * row order: idle, thinking, working, asking, happy, error, sleeping
    * frame index = row * 4 + column

The character is a neutral "hive spirit": a hexagon head in nvHive green with
a dark visor and glowing eyes, a stubby body, and a small antenna. It is a
deliberate placeholder — replace sheet.png with an approved sprite of the same
grid and the WebUI picks it up without a rebuild (see docs/MASCOT.md).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

FW = 64
FH = 64
COLS = 4
STATES = ["idle", "thinking", "working", "asking", "happy", "error", "sleeping"]

# Palette — nvHive brand green + the WebUI's neutral ramps.
GREEN = (0x76, 0xB9, 0x00, 255)
GREEN_DIM = (0x5A, 0x91, 0x00, 255)
GREEN_DEEP = (0x44, 0x70, 0x00, 255)
GREEN_LIGHT = (0x9A, 0xD0, 0x2E, 255)
VISOR = (0x0D, 0x0D, 0x0D, 255)
VISOR_EDGE = (0x2A, 0x2A, 0x2A, 255)
EYE = (0xD4, 0xF1, 0xC2, 255)
EYE_DIM = (0x76, 0xB9, 0x00, 255)
WHITE = (0xFA, 0xFA, 0xFA, 255)
RED = (0xDC, 0x26, 0x26, 255)
RED_DARK = (0x7F, 0x1D, 0x1D, 255)
AMBER = (0xD9, 0x77, 0x06, 255)
STEEL = (0xA3, 0xA3, 0xA3, 255)
STEEL_DARK = (0x52, 0x52, 0x52, 255)

# Character anchor points inside a 64x64 frame.
CX, CY, R = 32, 27, 16  # head hexagon centre + vertical radius
EYE_L, EYE_R = CX - 8, CX + 5  # left x of each 3px-wide eye

Color = tuple[int, int, int, int]


class Canvas:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.px = bytearray(w * h * 4)

    def put(self, x: int, y: int, c: Color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.px[i : i + 4] = bytes(c)

    def hline(self, x0: int, x1: int, y: int, c: Color) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.put(x, y, c)

    def vline(self, x: int, y0: int, y1: int, c: Color) -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.put(x, y, c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: Color) -> None:
        for y in range(y0, y1 + 1):
            self.hline(x0, x1, y, c)

    def rounded_rect(self, x0: int, y0: int, x1: int, y1: int, fill: Color, outline: Color) -> None:
        self.rect(x0, y0, x1, y1, outline)
        self.rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill)
        for (x, y) in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.put(x, y, (0, 0, 0, 0))

    def line(self, x0: int, y0: int, x1: int, y1: int, c: Color, thick: int = 1) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.put(x0, y0, c)
            if thick > 1:
                self.put(x0 + 1, y0, c)
                self.put(x0, y0 + 1, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def hexagon(self, cx: int, cy: int, r: int, c: Color) -> None:
        """Pointy-top regular hexagon, filled."""
        hw_full = r * 0.8660254
        for dy in range(-r, r + 1):
            ady = abs(dy)
            hw = hw_full if ady <= r / 2 else hw_full * (r - ady) / (r / 2)
            self.hline(int(round(cx - hw)), int(round(cx + hw)), cy + dy, c)

    def glyph(self, x: int, y: int, rows: list[str], c: Color) -> None:
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch == "#":
                    self.put(x + i, y + j, c)


# Tiny bitmap glyphs for the effects layer.
G_QMARK = [".##.", "#..#", "...#", "..#.", ".#..", "....", ".#.."]
G_EXCL = ["#", "#", "#", "#", "#", ".", "#"]
G_Z_SMALL = ["###", ".#.", "###"]
G_Z_BIG = ["####", "...#", "..#.", ".#..", "####"]
G_SPARK = [".#.", "###", ".#."]
G_SPARK_BIG = ["..#..", "..#..", "#####", "..#..", "..#.."]


def draw_eyes(c: Canvas, kind: str, dx: int, dy: int) -> None:
    for ex in (EYE_L, EYE_R):
        x, y = ex + dx, CY + dy
        if kind == "open":
            c.rect(x, y - 2, x + 2, y, EYE)
            c.put(x, y - 2, WHITE)
        elif kind == "up":
            c.rect(x, y - 3, x + 2, y - 1, EYE)
            c.put(x, y - 3, WHITE)
        elif kind == "half":
            c.rect(x, y - 1, x + 2, y, EYE)
        elif kind == "closed":
            c.hline(x, x + 2, y, EYE_DIM)
        elif kind == "sleep":
            c.put(x, y - 1, EYE_DIM)
            c.put(x + 1, y, EYE_DIM)
            c.put(x + 2, y - 1, EYE_DIM)
        elif kind == "happy":
            c.put(x + 1, y - 2, EYE)
            c.put(x, y - 1, EYE)
            c.put(x + 2, y - 1, EYE)
        elif kind == "x":
            for (i, j) in ((0, -2), (2, -2), (1, -1), (0, 0), (2, 0)):
                c.put(x + i, y + j, RED)


def draw_body(c: Canvas, dx: int, dy: int, *, right_arm: bool = True) -> None:
    # Legs + feet first so the body overlaps them.
    for lx in (27, 35):
        c.rect(lx + dx, 54 + dy, lx + 2 + dx, 57 + dy, GREEN_DEEP)
        c.hline(lx - 1 + dx, lx + 3 + dx, 57 + dy, GREEN_DEEP)
    c.rounded_rect(24 + dx, 40 + dy, 40 + dx, 53 + dy, GREEN_DIM, GREEN_DEEP)
    c.rect(31 + dx, 47 + dy, 32 + dx, 48 + dy, GREEN_LIGHT)  # chest light
    # Arms: 3px wide, hands darker.
    c.rect(20 + dx, 44 + dy, 22 + dx, 49 + dy, GREEN_DIM)
    c.rect(20 + dx, 50 + dy, 22 + dx, 51 + dy, GREEN_DEEP)
    if right_arm:
        c.rect(42 + dx, 44 + dy, 44 + dx, 49 + dy, GREEN_DIM)
        c.rect(42 + dx, 50 + dy, 44 + dx, 51 + dy, GREEN_DEEP)


def draw_head(c: Canvas, dx: int, dy: int, *, bulb: Color, visor_edge: Color, brow_lift: int | None = None,
              mouth: bool = False) -> None:
    cx, cy = CX + dx, CY + dy
    # Antenna.
    c.vline(cx, cy - R - 3, cy - R - 1, GREEN_DEEP)
    c.rect(cx - 1, cy - R - 5, cx, cy - R - 4, bulb)
    # Head: dark outline, green fill, light catch along the upper-left edge.
    c.hexagon(cx, cy, R, GREEN_DEEP)
    c.hexagon(cx, cy, R - 1, GREEN)
    hw_full = (R - 1) * 0.8660254
    for d in range(-(R - 1) + 2, -(R // 2)):
        hw = hw_full * ((R - 1) - abs(d)) / ((R - 1) / 2)
        c.put(int(round(cx - hw)) + 1, cy + d, GREEN_LIGHT)
    # Visor band with rounded ends.
    c.rect(cx - 11, cy - 5, cx + 11, cy + 3, VISOR)
    c.hline(cx - 11, cx + 11, cy - 5, visor_edge)
    c.hline(cx - 11, cx + 11, cy + 3, visor_edge)
    for (x, y) in ((cx - 11, cy - 5), (cx + 11, cy - 5), (cx - 11, cy + 3), (cx + 11, cy + 3)):
        c.put(x, y, GREEN)
    if brow_lift is not None:
        c.hline(EYE_L - 1 + dx, EYE_L + 2 + dx, cy - 7 - brow_lift, GREEN_DEEP)
    if mouth:
        c.hline(cx - 2, cx + 2, cy + 6, GREEN_DEEP)
        c.put(cx - 3, cy + 5, GREEN_DEEP)
        c.put(cx + 3, cy + 5, GREEN_DEEP)


def draw_wrench_arm(c: Canvas, dx: int, dy: int, hand: tuple[int, int], spark: bool) -> None:
    sx, sy = 43 + dx, 45 + dy
    hx, hy = hand[0] + dx, hand[1] + dy
    c.line(sx, sy, hx, hy, GREEN_DIM, thick=2)
    c.rect(hx - 1, hy - 1, hx + 1, hy + 1, GREEN_DEEP)
    # Wrench: shaft continues past the hand, open ring at the tip.
    vx, vy = hx - sx, hy - sy
    norm = max(1.0, (vx * vx + vy * vy) ** 0.5)
    tx, ty = int(round(hx + vx / norm * 7)), int(round(hy + vy / norm * 7))
    c.line(hx, hy, tx, ty, STEEL, thick=2)
    c.rect(tx - 2, ty - 2, tx + 2, ty + 2, STEEL)
    c.rect(tx - 1, ty - 1, tx + 1, ty + 1, STEEL_DARK)
    c.put(tx, ty, (0, 0, 0, 0))
    c.put(tx + (1 if vx >= 0 else -1) * 2, ty, (0, 0, 0, 0))
    if spark:
        c.glyph(tx + 3, ty - 4, G_SPARK, AMBER)


def draw_frame(c: Canvas, state: str, i: int) -> None:
    """Draw frame `i` (0..3) of `state` at the canvas origin (0, 0)."""
    dx = dy = 0
    if state == "idle":
        dy = (0, 0, -1, 0)[i]
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=GREEN_LIGHT, visor_edge=VISOR_EDGE)
        draw_eyes(c, ("open", "open", "half", "closed")[i], dx, dy)
    elif state == "thinking":
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=(GREEN_LIGHT, WHITE, GREEN_LIGHT, GREEN_DIM)[i], visor_edge=VISOR_EDGE)
        draw_eyes(c, "up", dx, dy)
        lit = (1, 2, 3, 0)[i]
        for n, (x, y) in enumerate(((50, 12), (55, 8), (60, 4))):
            c.rect(x, y, x + 1, y + 1, GREEN_LIGHT if n < lit else GREEN_DEEP)
    elif state == "working":
        draw_body(c, dx, dy, right_arm=False)
        draw_head(c, dx, dy, bulb=GREEN_LIGHT, visor_edge=VISOR_EDGE)
        draw_eyes(c, "half", dx, dy)
        draw_wrench_arm(c, dx, dy, ((50, 36), (53, 42), (51, 49), (53, 42))[i], spark=(i == 2))
    elif state == "asking":
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=GREEN_LIGHT, visor_edge=VISOR_EDGE, brow_lift=(0, 1, 1, 0)[i])
        draw_eyes(c, "open", dx, dy)
        c.glyph(49, 4 + (0, -1, 0, -1)[i], G_QMARK, (GREEN_LIGHT, WHITE, GREEN_LIGHT, WHITE)[i])
    elif state == "happy":
        dy = (0, -4, -6, -2)[i]
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=WHITE, visor_edge=VISOR_EDGE, mouth=True)
        draw_eyes(c, "happy", dx, dy)
        if i == 1:
            c.glyph(12, 12, G_SPARK, WHITE)
        if i >= 2:
            c.glyph(49, 6, G_SPARK_BIG, GREEN_LIGHT)
        if i == 3:
            c.glyph(10, 30, G_SPARK, GREEN_LIGHT)
    elif state == "error":
        dx = (-2, 2, -1, 1)[i]
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=RED, visor_edge=RED_DARK)
        draw_eyes(c, "x", dx, dy)
        if i in (0, 1):
            c.glyph(52, 4, G_EXCL, RED)
    elif state == "sleeping":
        dy = (0, 1, 1, 0)[i]
        draw_body(c, dx, dy)
        draw_head(c, dx, dy, bulb=GREEN_DIM, visor_edge=VISOR_EDGE)
        draw_eyes(c, "sleep", dx, dy)
        zs = (
            ((48, 14, G_Z_SMALL),),
            ((48, 12, G_Z_SMALL), (53, 6, G_Z_SMALL)),
            ((48, 10, G_Z_SMALL), (53, 4, G_Z_SMALL), (57, 0, G_Z_BIG)),
            ((53, 3, G_Z_SMALL), (57, 0, G_Z_BIG)),
        )[i]
        for n, (x, y, g) in enumerate(zs):
            c.glyph(x, y, g, GREEN_LIGHT if n == len(zs) - 1 else GREEN_DIM)
    else:  # pragma: no cover - guarded by STATES
        raise ValueError(state)


def render_sheet() -> Canvas:
    sheet = Canvas(FW * COLS, FH * len(STATES))
    for row, state in enumerate(STATES):
        for col in range(COLS):
            frame = Canvas(FW, FH)
            draw_frame(frame, state, col)
            # Blit (opaque pixels only) into the sheet.
            for y in range(FH):
                for x in range(FW):
                    i = (y * FW + x) * 4
                    if frame.px[i + 3]:
                        sheet.put(col * FW + x, row * FH + y, tuple(frame.px[i : i + 4]))  # type: ignore[arg-type]
    return sheet


def write_png(path: str, c: Canvas) -> None:
    raw = b"".join(b"\x00" + bytes(c.px[y * c.w * 4 : (y + 1) * c.w * 4]) for y in range(c.h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", c.w, c.h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--out", default=os.path.join(here, "sheet.png"), help="output PNG path")
    args = parser.parse_args(argv)
    sheet = render_sheet()
    write_png(args.out, sheet)
    print(f"wrote {args.out}: {sheet.w}x{sheet.h}, {COLS} cols x {len(STATES)} rows of {FW}x{FH}")
    for row, state in enumerate(STATES):
        print(f"  row {row} {state:9s} frames {row * COLS}-{row * COLS + COLS - 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
