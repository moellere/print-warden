"""Bed pose for the bed check. At slice time the machine's end gcode is edited so every warden print ends parked at
the configured pose with the steppers still holding it (Klipper's idle_timeout releases them later); at check time
the live toolhead must be at that pose, or the check is skipped rather than compared against the wrong framing."""

from __future__ import annotations

import re
from typing import Any

from .config import Pose

MARKER = "; PRINT_WARDEN_PARK"
TOLERANCE_MM = 1.0
Z_CLEARANCE_MM = 10.0
_STEPPERS_OFF = re.compile(r"^\s*(M84|M18)\b", re.IGNORECASE)


def prepare_end_gcode(template: str) -> str:
    """Mark where the park goes (before the first stepper-off, else first) and drop the stepper-off lines."""
    lines = template.splitlines()
    first = next((i for i, ln in enumerate(lines) if _STEPPERS_OFF.match(ln)), None)
    out = []
    for i, ln in enumerate(lines):
        if i == first:
            out.append(MARKER)
        if _STEPPERS_OFF.match(ln):
            out.append(
                f"; print-warden: '{ln.strip()}' removed, idle_timeout releases the steppers"
            )
        else:
            out.append(ln)
    if first is None:
        out.insert(0, MARKER)
    return "\n".join(out)


def park_z(pose: Pose, max_z: float | None, height_limit: float | None) -> float:
    """At least the pose's Z, at least clear of the part, never above the machine."""
    z = max(pose.z, (max_z or 0) + Z_CLEARANCE_MM)
    return min(z, height_limit) if height_limit else z


def rehome_script(pose: Pose) -> str:
    """Home only the horizontal axes the pose needs, then move there. Z never moves."""
    axes = "X Y" if pose.x is not None else "Y"
    xy = f"X{pose.x:.2f} Y{pose.y:.2f}" if pose.x is not None else f"Y{pose.y:.2f}"
    return f"G28 {axes}\nG90\nG1 {xy} F3000\nM400"


def park_block(pose: Pose, z: float) -> str:
    xy = f"X{pose.x:.2f} Y{pose.y:.2f}" if pose.x is not None else f"Y{pose.y:.2f}"
    return "\n".join(
        [
            "; print-warden: park for the bed check",
            "SAVE_GCODE_STATE NAME=print_warden_park",
            "G90",
            f"G1 Z{z:.2f} F600",
            f"G1 {xy} F3000",
            "M400",
            "RESTORE_GCODE_STATE NAME=print_warden_park",
        ]
    )


def apply(
    gcode: str, pose: Pose, max_z: float | None, height_limit: float | None
) -> tuple[str, list[str]]:
    """Replace the marker the edited end gcode left in the sliced file. Returns (gcode, warnings)."""
    if MARKER not in gcode:
        return gcode, [
            "park marker missing from the gcode; the bed will not be parked for the bed check"
        ]
    z = park_z(pose, max_z, height_limit)
    warnings = []
    if z > pose.z:
        warnings.append(
            f"part height {max_z} mm: parked at Z{z:.0f}, above pose Z{pose.z:.0f}"
        )
    if height_limit and (max_z or 0) + Z_CLEARANCE_MM > height_limit:
        warnings.append(
            f"part too tall to clear by {Z_CLEARANCE_MM:.0f} mm under the machine's height limit"
        )
    return gcode.replace(MARKER, park_block(pose, z), 1), warnings


def status(
    toolhead: dict[str, Any], pose: Pose, trusted_z: float | None = None
) -> tuple[bool, str]:
    """Is the toolhead at the pose right now? Position only counts on homed axes; `trusted_z` stands in for an
    unhomed Z after the warden rehomed Y alone (the lead screws hold Z with the motors off)."""
    homed = (toolhead.get("homed_axes") or "").lower()
    pos = list(toolhead.get("position") or [])
    if len(pos) < 3:
        return False, "toolhead position unknown"
    if "z" not in homed and trusted_z is not None:
        homed += "z"
        pos[2] = trusted_z
    need = "xyz" if pose.x is not None else "yz"
    if any(a not in homed for a in need):
        return (
            False,
            f"steppers released (homed: '{homed or 'none'}'); bed position unknown",
        )
    if abs(pos[1] - pose.y) > TOLERANCE_MM:
        return False, f"bed at Y{pos[1]:.1f}, not the check pose Y{pose.y:.1f}"
    if pose.x is not None and abs(pos[0] - pose.x) > TOLERANCE_MM:
        return False, f"head at X{pos[0]:.1f}, not the check pose X{pose.x:.1f}"
    if pos[2] < pose.z - TOLERANCE_MM:
        return False, f"gantry at Z{pos[2]:.1f}, below the check pose Z{pose.z:.1f}"
    return True, "at pose"
