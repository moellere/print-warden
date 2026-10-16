"""Deterministic start-print policy. No model is consulted here; it only reads facts already established."""

from __future__ import annotations

from typing import Any

IDLE_STATES = {"standby", "complete", "cancelled", None}
SPOOL_MARGIN = 1.1


def can_start(
    job: dict[str, Any],
    status: dict[str, Any],
    loaded_material: str | None,
    require_material: bool = True,
    has_power_device: bool = False,
    spool_remaining_g: float | None = None,
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
    need = job.get("filament_g")
    if (
        spool_remaining_g is not None
        and need
        and spool_remaining_g < need * SPOOL_MARGIN
    ):
        reasons.append(
            f"spool has {spool_remaining_g:.0f} g left, job needs {need:.0f} g (+{SPOOL_MARGIN - 1:.0%} margin)"
        )
    return (not reasons, reasons)


def approval_expired(job: dict[str, Any], now: float, ttl_minutes: float) -> bool:
    """A phone tap answers one request; once the request is older than the TTL the bed may have changed."""
    asked = job.get("approval_requested_at")
    return asked is None or now - asked > ttl_minutes * 60
