"""Job preview drawn from the gcode: a top-down view of every extrusion move on the bed outline, shaded from dark
(first layers) to light (top). Used when the slicer exports no thumbnail; headless OrcaSlicer 2.4.2 never does, because
its CLI asks GLFW for an OpenGL context numbered after GLFW's own library version (3.4), which does not exist."""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from .checks import AXIS

SIZE = 480
MARGIN = 16
_MOVE = re.compile(r"^G[01]\s", re.IGNORECASE)


def segments(text: str) -> list[tuple[float, float, float, float, float]]:
    """(x0, y0, x1, y1, z) for every move that extrudes, in file order."""
    x: float | None = None
    y: float | None = None
    z = 0.0
    e = 0.0
    relative = False
    out: list[tuple[float, float, float, float, float]] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        up = line[:4].upper()
        if up.startswith("M83"):
            relative = True
        elif up.startswith("M82"):
            relative = False
        elif up.startswith("G92"):
            for k, v in AXIS.findall(line):
                if k.upper() == "E":
                    e = float(v)
        elif _MOVE.match(line):
            ax = {k.upper(): float(v) for k, v in AXIS.findall(line)}
            nx, ny, z = ax.get("X", x), ax.get("Y", y), ax.get("Z", z)
            extrudes = False
            if "E" in ax:
                de = ax["E"] if relative else ax["E"] - e
                if not relative:
                    e = ax["E"]
                extrudes = de > 0
            if (
                extrudes
                and x is not None
                and y is not None
                and nx is not None
                and ny is not None
            ):
                out.append((x, y, nx, ny, z))
            x, y = nx, ny
    return out


def render(text: str, bed: tuple[float, float, float, float], out: Path) -> Path | None:
    """Draw the gcode's extrusion onto the bed rect (x0, y0, x1, y1); None when there is nothing to draw."""
    segs = segments(text)
    if not segs:
        return None
    bx0, by0, bx1, by1 = bed
    scale = (SIZE - 2 * MARGIN) / max(bx1 - bx0, by1 - by0)
    w = int((bx1 - bx0) * scale) + 2 * MARGIN
    h = int((by1 - by0) * scale) + 2 * MARGIN

    def px(x: float, y: float) -> tuple[float, float]:
        return MARGIN + (x - bx0) * scale, h - MARGIN - (
            y - by0
        ) * scale  # bed Y up, image Y down

    im = Image.new("RGB", (w, h), (32, 34, 38))
    d = ImageDraw.Draw(im)
    d.rectangle(
        [px(bx0, by1), px(bx1, by0)],
        fill=(52, 56, 62),
        outline=(120, 126, 134),
        width=2,
    )
    zmax = max(s[4] for s in segs) or 1.0
    for x0, y0, x1, y1, z in segs:
        t = z / zmax
        colour = (int(190 + 65 * t), int(80 + 140 * t), int(30 + 90 * t))
        d.line([px(x0, y0), px(x1, y1)], fill=colour, width=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return out
