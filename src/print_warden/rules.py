"""Deterministic telemetry rules. Input: merged Moonraker state; output: Events. No model, no I/O."""

from __future__ import annotations

import time
from typing import Any

from .config import Rules
from .events import Event

FINAL = {
    "complete": ("print_complete", "info"),
    "cancelled": ("print_cancelled", "warning"),
    "error": ("print_error", "critical"),
}


class RuleEngine:
    def __init__(self, printer: str, rules: Rules):
        self.printer = printer
        self.rules = rules
        self.prev_state: str | None = None
        self.prev_klippy: str | None = None
        self.prev_layer: int | None = None
        self.heater_since: dict[str, float] = {}
        self.heater_target: dict[str, float] = {}
        self.heater_settled: set[str] = set()
        self.heater_alerted: set[str] = set()
        self.last_progress: tuple[float, float] | None = None
        self.stall_alerted = False
        self.max_z: float = -1.0
        self.z_layer: int = 0
        self.unreachable_since: float | None = None
        self.unreachable_alerted = False

    def _ev(
        self, kind: str, severity: str, message: str, alert: bool = True, **data: Any
    ) -> Event:
        return Event(self.printer, kind, severity, message, data, alert=alert)

    def evaluate(
        self, st: dict[str, Any], signal: str | None, now: float | None = None
    ) -> list[Event]:
        now = now or time.time()
        out: list[Event] = []
        ps = st.get("print_stats", {})
        state = ps.get("state")
        fn = ps.get("filename") or ""

        if signal == "transport_lost":
            # Reconnects usually succeed within seconds; alert only once the outage has lasted.
            if self.unreachable_since is None:
                self.unreachable_since = now
                out.append(
                    self._ev(
                        "moonraker_unreachable",
                        "debug",
                        "Lost connection to Moonraker",
                        alert=False,
                    )
                )
            elif now - self.unreachable_since > 60 and not self.unreachable_alerted:
                self.unreachable_alerted = True
                out.append(
                    self._ev(
                        "moonraker_unreachable",
                        "critical",
                        f"Moonraker unreachable for {now - self.unreachable_since:.0f}s",
                    )
                )
            return out
        if self.unreachable_since is not None:
            gap = now - self.unreachable_since
            self.unreachable_since = None
            self.unreachable_alerted = False
            out.append(
                self._ev(
                    "moonraker_reconnected",
                    "info",
                    f"Moonraker back after {gap:.0f}s",
                    alert=gap > 60,
                )
            )
        if signal == "notify_klippy_disconnected":
            out.append(
                self._ev(
                    "klippy_disconnected",
                    "critical",
                    "Klipper disconnected from Moonraker",
                )
            )
        elif signal == "notify_klippy_shutdown":
            out.append(
                self._ev(
                    "klippy_shutdown",
                    "critical",
                    f"Klipper shutdown: {st.get('webhooks', {}).get('state_message', '')[:200]}",
                )
            )
        elif signal == "notify_klippy_ready":
            out.append(self._ev("klippy_ready", "info", "Klipper ready", alert=False))

        if state != self.prev_state and self.prev_state is not None:
            if state == "printing" and self.prev_state == "paused":
                out.append(
                    self._ev("print_resumed", "info", f"Resumed {fn}", alert=False)
                )
            elif state == "printing":
                out.append(
                    self._ev("print_started", "info", f"Started {fn}", filename=fn)
                )
                self._reset_job()
            elif state == "paused":
                out.append(
                    self._ev(
                        "print_paused",
                        "warning",
                        f"Paused {fn}: {ps.get('message') or ''}",
                    )
                )
            elif state in FINAL:
                kind, sev = FINAL[state]
                dur = ps.get("print_duration") or 0
                out.append(
                    self._ev(
                        kind,
                        sev,
                        f"{state.capitalize()} {fn} after {dur / 3600:.1f} h: {ps.get('message') or ''}",
                        filename=fn,
                        print_duration=dur,
                    )
                )
        self.prev_state = state

        if state == "printing":
            out.extend(self._heaters(st, now))
            out.extend(self._stall(st, now))
            layer = (ps.get("info") or {}).get("current_layer")
            if not isinstance(layer, int):
                layer = self._layer_from_z(st)
            if isinstance(layer, int) and layer != self.prev_layer:
                out.append(
                    self._ev(
                        "layer_changed",
                        "debug",
                        f"Layer {layer}",
                        alert=False,
                        layer=layer,
                        total=(ps.get("info") or {}).get("total_layer"),
                    )
                )
                self.prev_layer = layer
        return out

    def _layer_from_z(self, st: dict[str, Any]) -> int | None:
        """Slicers that never emit SET_PRINT_STATS_INFO leave current_layer unset; count new Z maxima instead.
        Z-hops go up and come back down, so only a Z that exceeds the previous maximum counts as a layer."""
        pos = st.get("gcode_move", {}).get("gcode_position")
        if not pos or len(pos) < 3:
            return None
        z = float(pos[2])
        if z > self.max_z + 0.02 and z < 500:
            self.max_z = z
            self.z_layer += 1
        return self.z_layer or None

    def _reset_job(self) -> None:
        self.max_z = -1.0
        self.z_layer = 0
        self.heater_since.clear()
        self.heater_alerted.clear()
        self.heater_settled.clear()
        self.heater_target.clear()
        self.last_progress = None
        self.stall_alerted = False
        self.prev_layer = None

    def _heaters(self, st: dict[str, Any], now: float) -> list[Event]:
        """Flag a heater that drifts after it has settled at its target. Heat-up, cool-down and target changes
        are transients, not faults, so nothing counts until the heater has first reached the target."""
        out = []
        for h in ("extruder", "heater_bed"):
            d = st.get(h, {})
            target, temp = d.get("target") or 0, d.get("temperature") or 0
            if target <= 0 or target != self.heater_target.get(h):
                self.heater_target[h] = target
                self.heater_settled.discard(h)
                self.heater_since.pop(h, None)
                self.heater_alerted.discard(h)
                continue
            within = abs(temp - target) <= self.rules.heater_deviation_c
            if h not in self.heater_settled:
                if within:
                    self.heater_settled.add(h)
                continue
            if not within:
                since = self.heater_since.setdefault(h, now)
                if (
                    now - since > self.rules.heater_deviation_s
                    and h not in self.heater_alerted
                ):
                    self.heater_alerted.add(h)
                    out.append(
                        self._ev(
                            "heater_deviation",
                            "critical",
                            f"{h} at {temp:.0f}°C vs target {target:.0f}°C for {now - since:.0f}s after settling",
                            heater=h,
                            temperature=temp,
                            target=target,
                        )
                    )
            else:
                self.heater_since.pop(h, None)
                self.heater_alerted.discard(h)
        return out

    def _stall(self, st: dict[str, Any], now: float) -> list[Event]:
        prog = float(st.get("virtual_sdcard", {}).get("progress") or 0)
        if self.last_progress is None or prog != self.last_progress[0]:
            self.last_progress = (prog, now)
            self.stall_alerted = False
            return []
        idle = now - self.last_progress[1]
        if idle > self.rules.stall_minutes * 60 and not self.stall_alerted:
            self.stall_alerted = True
            return [
                self._ev(
                    "progress_stall",
                    "warning",
                    f"No progress for {idle / 60:.0f} min at {prog * 100:.1f}%",
                    progress=prog,
                )
            ]
        return []
