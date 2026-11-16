"""Why a print stopped with an error, from what Klipper said, and what to check.

A print that ends in Klipper's `error` state leaves its reason in three places: `print_stats.message`, the webhooks
`state_message` when Klipper shut down, and the `!! ` lines of Moonraker's gcode store, which also hold the last
command sent. `classify` matches those against the print errors below, then the shutdown causes (`rules`), first
match wins. The cause lands on the job (`failure_cause`, `failure_detail`), leads the phone prompt, feeds
`printer_stats`, and warns on `submit_job` when it keeps happening on one printer.
"""

from __future__ import annotations

from typing import Any

from .rules import SHUTDOWN_CAUSES, UNKNOWN_HINT

# gcode errors Klipper reports without shutting down -> (cause, what to check). First match wins.
ERROR_CAUSES: tuple[tuple[str, str, str], ...] = (
    (
        "must home axis first",
        "not_homed",
        "the gcode moved before homing: check that the start gcode (or PRINT_START) runs G28",
    ),
    (
        "move out of range",
        "move_out_of_range",
        "a move went past an axis limit: check the model's placement, the profile's bed size and the start/end gcode",
    ),
    (
        "extrude below minimum temp",
        "cold_extrusion",
        "the nozzle was below min_extrude_temp: check the start gcode heats before it extrudes, and the heater",
    ),
    (
        "extrude only move too long",
        "extrude_too_long",
        "one extrusion exceeds max_extrude_only_distance: a purge or retraction too long for the [extruder] limit",
    ),
    (
        "move exceeds maximum extrusion",
        "extrusion_too_fast",
        "an extrusion is too wide or fast for max_extrude_cross_section: check line width and flow in the slicer",
    ),
    (
        "unknown command",
        "unknown_command",
        "the gcode calls a command this Klipper lacks: a macro the profile expects is missing from printer.cfg",
    ),
    (
        "probe triggered prior to movement",
        "probe_fault",
        "the probe read triggered before it moved: check the probe, its pin and its wiring",
    ),
    (
        "no trigger on probe",
        "probe_fault",
        "the probe never triggered: check the probe, its wiring and the Z offset",
    ),
    (
        "unable to open file",
        "file_error",
        "the printer could not read the gcode file: re-upload it (Reprint does)",
    ),
)
HINTS = {cause: hint for _, cause, hint in (*ERROR_CAUSES, *SHUTDOWN_CAUSES)}


def classify(text: str) -> tuple[str, str]:
    """(cause, what to check) for Klipper's error text; ("unknown", ...) when nothing matches."""
    low = text.lower()
    for needle, cause, hint in (*ERROR_CAUSES, *SHUTDOWN_CAUSES):
        if needle in low:
            return cause, hint
    return "unknown", UNKNOWN_HINT


def from_gcode_store(lines: list[dict[str, Any]]) -> tuple[list[str], str | None]:
    """The `!! ` error responses (oldest first) and the last command sent, from Moonraker's gcode store."""
    errors = [
        str(ln.get("message") or "")[3:].strip()
        for ln in lines
        if ln.get("type") == "response"
        and str(ln.get("message") or "").startswith("!!")
    ]
    commands = [
        str(ln.get("message") or "") for ln in lines if ln.get("type") == "command"
    ]
    return errors, (commands[-1] if commands else None)


def diagnose(messages: list[str], store: list[dict[str, Any]]) -> dict[str, str]:
    """The job fields for a print that stopped with an error: failure_cause and failure_detail (what Klipper said
    and the last command sent)."""
    errors, last = from_gcode_store(store)
    said = [m for m in (*messages, *errors[-3:]) if m]
    cause, _ = classify(" | ".join(said))
    detail = (
        errors[-1] if errors else next((m for m in messages if m), "")
    ) or "no message"
    if last:
        detail += f" (last command: {last})"
    return {"failure_cause": cause, "failure_detail": detail[:400]}


def label(cause: str) -> str:
    return cause.replace("_", " ")
