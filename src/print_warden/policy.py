"""Deterministic start-print policy. No model is consulted here; it only reads facts already established."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bedcheck import Check

IDLE_STATES = {"standby", "complete", "cancelled", None}
SPOOL_MARGIN = 1.1


def can_start(
    job: dict[str, Any],
    status: dict[str, Any],
    loaded_material: str | None,
    require_material: bool = True,
    has_power_device: bool = False,
    spool_remaining_g: float | None = None,
    bed_state: str | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for b in job.get("slice_blockers") or []:
        reasons.append(
            f"slice check: {b} (waive_blockers with the operator's consent to print anyway)"
        )
    if bed_state == "occupied":
        reasons.append(
            "bed not cleared since the last print; tap Bed cleared or mark_bed_clear"
        )
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


def in_window(spec: str | None, now: datetime) -> bool:
    """`spec` is "HH:MM-HH:MM" in local time; a window may wrap past midnight. No spec means no window."""
    if not spec:
        return False
    start_s, _, end_s = spec.partition("-")
    start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
    t = now.time()
    return start <= t < end if start <= end else t >= start or t < end


def finish_ok(window: str | None, now: datetime, est_seconds: float | None) -> bool:
    """Would a print started now finish inside `window`? No window means any finish time is fine."""
    if not window:
        return True
    if not est_seconds:
        return False
    return in_window(window, now + timedelta(seconds=est_seconds))


def can_auto_start(
    bed_state: str,
    current: Check | None,
    history: list[Check],
    min_agreements: int,
    hours: str | None,
    now: datetime,
    skipped: str | None = None,
) -> tuple[bool, list[str]]:
    """The extra gate for starting without a tap. `history` is labelled checks, newest first; the check's
    record must be clean for the last `min_agreements` times a human knew the answer."""
    reasons: list[str] = []
    if bed_state != "clear":
        reasons.append(f"bed is {bed_state}; a human has to mark it clear first")
    if current is None:
        reasons.append(
            f"no bed check: {skipped or 'camera or empty-bed reference missing'}"
        )
    elif not current.clear:
        reasons.append(
            f"bed check sees something: {current.score:.1%} of the bed differs"
        )
    recent = history[:min_agreements]
    if len(recent) < min_agreements:
        reasons.append(f"bed check has {len(recent)}/{min_agreements} labelled checks")
    elif not all(c.agrees for c in recent):
        wrong = sum(1 for c in recent if not c.agrees)
        reasons.append(
            f"bed check disagreed with the operator {wrong}x in the last {min_agreements}"
        )
    if not in_window(hours, now):
        reasons.append(f"outside auto_start_hours ({hours or 'not set'})")
    return (not reasons, reasons)
