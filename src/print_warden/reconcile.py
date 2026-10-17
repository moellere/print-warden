"""Restart recovery: decide what became of a job the warden lost track of, from Moonraker's live status and print
history. Pure function; the watcher applies the verdict through the job ledger's transition table."""

from __future__ import annotations

from typing import Any

HISTORY_DONE = {"completed"}
HISTORY_FAILED = {
    "cancelled",
    "error",
    "klippy_shutdown",
    "klippy_disconnect",
    "interrupted",
    "server_exit",
}
PENDING = ("staged", "awaiting_approval", "approved")


def decide(
    job: dict[str, Any], status: dict[str, Any], history: list[dict[str, Any]]
) -> tuple[str, str, float | None, float | None] | None:
    """Return (new_state, note, filament_used_mm, print_duration_s) or None when the job's state still holds."""
    remote = job.get("remote_name") or ""
    on_printer = status.get("filename") == remote and status.get("state") in (
        "printing",
        "paused",
    )
    if job["state"] in PENDING:
        if on_printer:
            return "printing", "found printing after warden restart", None, None
        if job["state"] == "approved":
            return "staged", "warden restarted mid-start; approve again", None, None
        return None
    if job["state"] != "printing" or on_printer:
        return None
    started = job.get("started_at") or 0
    for h in history:
        if h.get("filename") != remote or (h.get("start_time") or 0) < started - 120:
            continue
        st = h.get("status")
        used, dur = h.get("filament_used"), h.get("print_duration")
        if st in HISTORY_DONE:
            return "done", "completed while the warden was away (history)", used, dur
        if st in HISTORY_FAILED:
            return "failed", f"{st} while the warden was away (history)", used, dur
        return None
    return (
        "failed",
        "outcome unknown: not printing and not in Moonraker history",
        None,
        None,
    )
