"""Calibration towers: one print per spool and setting, the best-looking section becomes the spool's value.

A tower of `SECTIONS` stacked sections is sliced like any job; after slicing, the warden inserts the section's
command at the first layer of each section, so one print tries five values:

- `temp`: nozzle temperature, `M104 S<t>`, from 10 °C above the spool's (or profile's) temperature down to 10 below.
- `flow`: extrusion multiplier, `M221 S<percent>`, 104 % down to 96 %; the chosen one is multiplied into the
  spool's flow ratio.
- `pa`: Klipper pressure advance, `SET_PRESSURE_ADVANCE ADVANCE=<v>`.

The values are fixed in code and checked after insertion like any slice (temperatures against the material's range).
After the print, the phone asks which section looked best; the answer is written to the spool in Spoolman
(`pw_nozzle_temp`, `pw_flow_ratio`, `pw_pressure_advance`) and applied to that spool's later jobs.
"""

from __future__ import annotations

import re
import struct
from typing import Any

SECTIONS = 5
SECTION_H = 8.0
SIZE = 20.0
KINDS = ("temp", "flow", "pa")
FIELDS = {
    "temp": "pw_nozzle_temp",
    "flow": "pw_flow_ratio",
    "pa": "pw_pressure_advance",
}
PA_DIRECT = (0.01, 0.025, 0.04, 0.055, 0.07)
PA_BOWDEN = (0.2, 0.35, 0.5, 0.65, 0.8)
_Z = re.compile(r"^G[01]\b.*?\bZ(-?\d*\.?\d+)", re.IGNORECASE)


def values(
    kind: str, base_temp: float | None = None, bowden: bool = False
) -> tuple[float, ...]:
    """What each section tries, bottom to top."""
    if kind == "temp":
        if not base_temp:
            raise ValueError(
                "a temperature tower needs the spool's or profile's nozzle temperature"
            )
        return tuple(float(base_temp + d) for d in (10, 5, 0, -5, -10))
    if kind == "flow":
        return (1.04, 1.02, 1.0, 0.98, 0.96)
    if kind == "pa":
        return PA_BOWDEN if bowden else PA_DIRECT
    raise ValueError(f"kind must be one of {KINDS}")


def command(kind: str, value: float) -> str:
    if kind == "temp":
        return f"M104 S{round(value)}"
    if kind == "flow":
        return f"M221 S{round(value * 100)}"
    return f"SET_PRESSURE_ADVANCE ADVANCE={value:.3f}"


def tower_stl(
    sections: int = SECTIONS, section_h: float = SECTION_H, size: float = SIZE
) -> bytes:
    """A square tower `sections * section_h` tall, as binary STL, stepping 0.5 mm inward at every section boundary so
    the sections can be told apart without an overhang (which would bring supports)."""
    tris: list[tuple[tuple[float, float, float], ...]] = []

    def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> None:
        p = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]  # fmt: skip
        for a, b, c in (
            (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
        ):  # fmt: skip
            tris.append((p[a], p[b], p[c]))

    for i in range(sections):
        inset = 0.5 * i
        box(
            inset, inset, i * section_h, size - inset, size - inset, (i + 1) * section_h
        )
    out = bytearray(b"print-warden calibration tower".ljust(80, b" "))
    out += struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0.0, 0.0, 0.0)
        for v in t:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    return bytes(out)


def restore(kind: str, pressure_advance: float | None) -> str | None:
    """What puts the printer back after the tower: Klipper keeps M221 and pressure advance until it restarts."""
    if kind == "flow":
        return "M221 S100"
    if kind == "pa" and pressure_advance is not None:
        return command("pa", pressure_advance)
    return None


def apply(
    gcode: str,
    kind: str,
    vals: tuple[float, ...],
    section_h: float = SECTION_H,
    reset: str | None = None,
) -> str:
    """Insert each section's command at the first layer at or above its height, and `reset` at the end. Layer
    heights come from Orca's `;Z:` marks (z-hops on travel moves would otherwise flip sections), else from Z moves;
    sections only go up."""
    lines = gcode.splitlines()
    marked = any(ln.startswith(";Z:") for ln in lines)
    out: list[str] = []
    current = -1
    started = False
    for line in lines:
        if line.startswith(";TYPE:") and not line.startswith(";TYPE:Custom"):
            started = True
        z = None
        if marked and line.startswith(";Z:"):
            z = float(line[3:])
        elif not marked and (m := _Z.match(line)):
            z = float(m[1])
        if z is not None and (started or marked):
            section = min(len(vals) - 1, int((z + 1e-6) // section_h))
            if section > current:
                current = section
                out.append(
                    f"{command(kind, vals[section])} ; print-warden calibration section {section + 1}"
                )
        out.append(line)
    if current < 0:
        raise ValueError("no printed layer found to calibrate")
    if reset:
        out.append(f"{reset} ; print-warden calibration reset")
    return "\n".join(out) + "\n"


def spec(
    kind: str, vals: tuple[float, ...], spool_id: int | None, reset: str | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "values": list(vals),
        "spool_id": spool_id,
        "section_h": SECTION_H,
        "reset": reset,
    }


def result(
    cal: dict[str, Any], section: int, current: dict[str, Any]
) -> tuple[str, float]:
    """(Spoolman field, value) for the chosen 1-based section; flow multiplies into the spool's current ratio."""
    vals = cal["values"]
    if not 1 <= section <= len(vals):
        raise ValueError(f"section must be 1-{len(vals)}")
    v = float(vals[section - 1])
    if cal["kind"] == "flow":
        v = round(v * float(current.get("flow_ratio") or 1.0), 4)
    return FIELDS[cal["kind"]], v


def label(kind: str, value: float) -> str:
    if kind == "temp":
        return f"{round(value)}°C"
    if kind == "flow":
        return f"{round(value * 100)}%"
    return f"PA {value:.3f}"


def choices(job_id: str, cal: dict[str, Any], page: int) -> list[tuple[str, str]]:
    """Buttons for "which section looked best", three to a page: 1, 2, More… then 3, 4, 5."""
    vals = cal["values"]

    def button(n: int) -> tuple[str, str]:
        where = " (bottom)" if n == 1 else " (top)" if n == len(vals) else ""
        return (
            f"PRINT_WARDEN_CALIB_{job_id}:{n}",
            f"{n}{where}: {label(cal['kind'], vals[n - 1])}",
        )

    if page == 1:
        return [button(1), button(2), (f"PRINT_WARDEN_CALIB_{job_id}:more", "More…")]
    return [button(n) for n in range(3, len(vals) + 1)]
