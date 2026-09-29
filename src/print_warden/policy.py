"""Deterministic start-print policy. No model is consulted here; it only reads facts already established."""

from __future__ import annotations

from typing import Any

IDLE_STATES = {"standby", "complete", "cancelled", None}


def can_start(
    job: dict[str, Any],
    status: dict[str, Any],
    loaded_material: str | None,
    require_material: bool = True,
    has_power_device: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if job["state"] not in ("staged", "approved", "awaiting_approval"):
        reasons.append(f"job is {job['state']}, not staged")
    if not job.get("remote_name"):
        reasons.append("gcode not uploaded to the printer")
    if not status.get("moonraker_connected"):
        reasons.append("Moonraker not connected")
    if status.get("klippy") != "ready" and not has_power_device:
        reasons.append(f"Klipper is {status.get('klippy')!r}, not ready")
    if status.get("state") not in IDLE_STATES and not (
        has_power_device and status.get("klippy") != "ready"
    ):
        reasons.append(f"printer is {status.get('state')}")
    if (
        require_material
        and (loaded_material or "").upper() != (job.get("material") or "").upper()
    ):
        reasons.append(
            f"loaded material is {loaded_material or 'unknown'}, job needs {job.get('material')}"
        )
    return (not reasons, reasons)
