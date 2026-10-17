"""Slice-time checks. Before slicing: the model's bounding box against the machine's printable area and height. After
slicing: where the gcode actually extrudes, the temperatures it sets against the material's range, and Orca log lines
the operator has marked as blocking. A blocker keeps the job from starting; it never edits the gcode."""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

# (nozzle min, nozzle max, bed min, bed max) in °C. Bed 0 means unheated and is always allowed.
MATERIAL_TEMPS: dict[str, tuple[int, int, int, int]] = {
    "PLA": (180, 235, 0, 75),
    "PETG": (215, 265, 50, 95),
    "ABS": (225, 275, 80, 115),
    "ASA": (225, 275, 80, 115),
    "TPU": (200, 250, 0, 70),
    "PLA-CF": (190, 240, 0, 75),
}
EDGE_TOLERANCE_MM = 1.0
_MOVE = re.compile(r"^G[01]\s", re.IGNORECASE)
_AXIS = re.compile(r"([XYZE])(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_TEMP = re.compile(r"^(M10[49]|M1[49]0)\b.*?\bS(\d+(?:\.\d+)?)", re.IGNORECASE)


def stl_bounds(path: Path) -> tuple[float, float, float]:
    """(dx, dy, dz) of an STL, ASCII or binary."""
    data = path.read_bytes()
    pts: list[tuple[float, float, float]] = []
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * count and count:
        for i in range(count):
            off = 84 + 50 * i + 12
            v = struct.unpack_from("<9f", data, off)
            pts.extend([(v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])])
    else:
        for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            pts.append((float(m[1]), float(m[2]), float(m[3])))
    if not pts:
        raise ValueError(f"{path.name}: no geometry found")
    xs, ys, zs = zip(*pts, strict=True)
    return max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)


def bed_rect(machine: dict[str, Any]) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of the printable area, from Orca's ["0x0", "300x0", ...] polygon."""
    pts = [
        tuple(float(v) for v in p.split("x"))
        for p in machine.get("printable_area") or []
    ]
    if not pts:
        raise ValueError("machine preset has no printable_area")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def fit_problems(
    dims: tuple[float, float, float], machine: dict[str, Any], copies: int = 1
) -> list[str]:
    """Why a model cannot fit, before slicing. Footprint may rotate 90°; copies must fit by area at 80% packing."""
    x0, y0, x1, y1 = bed_rect(machine)
    bw, bh = x1 - x0, y1 - y0
    height = float(machine.get("printable_height") or 0)
    dx, dy, dz = dims
    out = []
    if not ((dx <= bw and dy <= bh) or (dy <= bw and dx <= bh)):
        out.append(
            f"footprint {dx:.0f}x{dy:.0f} mm does not fit the {bw:.0f}x{bh:.0f} mm bed"
        )
    if height and dz > height:
        out.append(f"height {dz:.0f} mm exceeds the printable height {height:.0f} mm")
    if copies > 1 and not out and copies * dx * dy > 0.8 * bw * bh:
        out.append(
            f"{copies} copies of {dx:.0f}x{dy:.0f} mm will not fit on one {bw:.0f}x{bh:.0f} mm plate"
        )
    return out


def gcode_facts(text: str) -> dict[str, Any]:
    """Extrusion extents and the temperatures the gcode sets (ignoring the 0s that switch heaters off). Extrusion only
    counts once the head has been sent to a known XY: a prime before the first move is at an unknown position."""
    x: float | None = None
    y: float | None = None
    e = 0.0
    relative_e = False
    ext: list[float] | None = None
    nozzle: list[float] = []
    bed: list[float] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        up = line.upper()
        if up.startswith("M83"):
            relative_e = True
        elif up.startswith("M82"):
            relative_e = False
        elif up.startswith("G92"):
            for k, v in _AXIS.findall(line):
                if k.upper() == "E":
                    e = float(v)
        elif m := _TEMP.match(line):
            t = float(m[2])
            if t > 0:
                (nozzle if m[1].upper() in ("M104", "M109") else bed).append(t)
        elif _MOVE.match(line):
            axes = {k.upper(): float(v) for k, v in _AXIS.findall(line)}
            x, y = axes.get("X", x), axes.get("Y", y)
            if "E" in axes:
                de = axes["E"] if relative_e else axes["E"] - e
                if not relative_e:
                    e = axes["E"]
                if de > 0 and x is not None and y is not None:
                    ext = (
                        [x, y, x, y]
                        if ext is None
                        else [
                            min(ext[0], x),
                            min(ext[1], y),
                            max(ext[2], x),
                            max(ext[3], y),
                        ]
                    )
    return {"extents": tuple(ext) if ext else None, "nozzle": nozzle, "bed": bed}


def gcode_problems(
    facts: dict[str, Any], machine: dict[str, Any], material: str
) -> list[str]:
    out = []
    if facts["extents"]:
        x0, y0, x1, y1 = bed_rect(machine)
        ex0, ey0, ex1, ey1 = facts["extents"]
        t = EDGE_TOLERANCE_MM
        if ex0 < x0 - t or ey0 < y0 - t or ex1 > x1 + t or ey1 > y1 + t:
            out.append(
                f"extrudes at X{ex0:.0f}..{ex1:.0f} Y{ey0:.0f}..{ey1:.0f}, outside the bed X{x0:.0f}..{x1:.0f} Y{y0:.0f}..{y1:.0f}"
            )
    rng = MATERIAL_TEMPS.get(material.upper())
    if rng:
        # Start gcode sets lower temperatures on the way up (a 140°C probing preheat, a nozzle wait while the bed
        # heats), so the printing temperature is the highest one the file sets.
        nmin, nmax, bmin, bmax = rng
        if facts["nozzle"] and not nmin <= max(facts["nozzle"]) <= nmax:
            out.append(
                f"nozzle {max(facts['nozzle']):.0f}°C is outside {material} range {nmin}-{nmax}°C"
            )
        if facts["bed"] and not bmin <= max(facts["bed"]) <= bmax:
            out.append(
                f"bed {max(facts['bed']):.0f}°C is outside {material} range {bmin}-{bmax}°C"
            )
    return out


def log_problems(log: str, patterns: tuple[str, ...]) -> list[str]:
    """Orca log lines containing an operator-chosen blocking pattern (case-insensitive)."""
    pats = [p.lower() for p in patterns]
    return [
        f"slicer: {ln.strip()[:200]}"
        for ln in log.splitlines()
        if any(p in ln.lower() for p in pats)
    ][:5]
