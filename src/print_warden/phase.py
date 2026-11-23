"""What a printer is doing, in words: the stage of a print or why it is idle, from its live status.

Klipper reports `printing` from the moment the file starts, through heat-up and the start macro (homing, probing,
a bed mesh, the purge), so a print at 0 % for minutes is normal and looked broken. `describe` splits that up:
- `heating`: a heater is still short of its target (bed within 2 °C, nozzle within 5 °C);
- `starting`: the heaters are at target but the nozzle is held below printing temperature (probing, a mesh), or
  no layer has begun yet;
- `printing`: a layer is reported (`SET_PRINT_STATS_INFO`), or, for files without layer info, progress has begun;
- `paused`, `cooling` (the print ended and the nozzle is still hot), `idle`, `offline` and Klipper's own states.
It reads only the status dict `PrinterWatcher.status()` returns, so it is pure and tested on plain dicts.
"""

from __future__ import annotations

from typing import Any

PRINT_TEMP_C = 170.0
"""A nozzle target below this during a print is a probing or standby temperature, not a printing one."""


def _t(h: dict[str, Any] | None) -> tuple[float, float]:
    h = h or {}
    return float(h.get("temperature") or 0), float(h.get("target") or 0)


def describe(st: dict[str, Any]) -> tuple[str, str]:
    """(key, words) for one printer's status."""
    if not st.get("moonraker_connected"):
        return "offline", "offline: Moonraker does not answer"
    klippy = st.get("klippy")
    if klippy and klippy != "ready":
        return "klipper", f"Klipper {klippy}" + (
            f": {st['klippy_message']}" if st.get("klippy_message") else ""
        )
    bed, bed_target = _t(st.get("bed"))
    noz, noz_target = _t(st.get("extruder"))
    state = st.get("state")
    if state == "paused":
        return "paused", "paused"
    if state == "printing":
        layer, total = st.get("layer"), st.get("total_layers")
        if layer:
            return "printing", f"printing layer {layer}" + (
                f" of {total}" if total else ""
            )
        short = []
        if bed_target > 0 and bed < bed_target - 2:
            short.append(f"bed {bed:.0f}/{bed_target:.0f} °C")
        if noz_target >= PRINT_TEMP_C and noz < noz_target - 5:
            short.append(f"nozzle {noz:.0f}/{noz_target:.0f} °C")
        if short:
            return "heating", "heating: " + ", ".join(short)
        if 0 < noz_target < PRINT_TEMP_C:
            return (
                "starting",
                f"start sequence: homing, probing or a bed mesh (nozzle held at {noz_target:.0f} °C)",
            )
        if float(st.get("progress_pct") or 0) >= 1:
            return "printing", f"printing, {float(st['progress_pct']):.0f} %"
        return "starting", "starting: purge and first moves"
    if noz > 50 and noz_target == 0 and state in ("complete", "cancelled", "error"):
        return "cooling", f"{state}; cooling (nozzle {noz:.0f} °C)"
    return "idle", "idle" if state in (None, "standby") else str(state)


def bed_note(
    st: dict[str, Any], bed: str | None, waiting: str | None = None
) -> str | None:
    """Why the status page shows no Bed cleared button for this printer, or None when it shows one."""
    printing = st.get("state") in ("printing", "paused")
    if bed == "occupied" and not printing:
        return None
    if printing:
        return "Bed cleared appears when this print ends."
    if bed == "clear":
        return (
            f"Bed is clear; {waiting} is waiting for your Start."
            if waiting
            else "Bed is clear."
        )
    return "Bed state unknown: it is set after the next print, or by Bed cleared on the phone."
