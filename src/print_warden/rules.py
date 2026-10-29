"""Deterministic telemetry rules. Input: merged Moonraker state; output: Events. No model, no I/O."""

from __future__ import annotations

import time
from typing import Any

from .config import Rules
from .events import Event

FAN_PREFIXES = ("fan", "heater_fan ", "controller_fan ", "fan_generic ")
RUNOUT_PREFIXES = ("filament_switch_sensor ", "filament_motion_sensor ")
TEMP_PREFIXES = ("temperature_sensor ", "temperature_fan ")
BOARD_HINTS = ("mcu", "host", "raspberry", "rpi", "pi_temp", "cpu")
BOARD_LIMIT_C = 75.0
TEMP_HYSTERESIS_C = 5.0

# Klipper shutdown messages -> (cause, what to check). First match wins; checked in order.
SHUTDOWN_CAUSES: tuple[tuple[str, str, str], ...] = (
    (
        "thermal runaway",
        "thermal_runaway",
        "a heater ran away from its target: check the thermistor and heater",
    ),
    (
        "not heating at expected rate",
        "heater_not_heating",
        "heater or thermistor fault, or a draft; check wiring",
    ),
    (
        "adc out of range",
        "thermistor_out_of_range",
        "thermistor open or shorted: check its wiring and connector",
    ),
    (
        "lost communication with mcu",
        "mcu_comm_lost",
        "USB/serial link to a board dropped: check cable and power",
    ),
    (
        "timer too close",
        "mcu_timing",
        "the host or MCU fell behind: check host load and USB",
    ),
    (
        "tmc",
        "tmc_driver_error",
        "a stepper driver reported an error: check motor wiring and driver cooling",
    ),
    (
        "move out of range",
        "move_out_of_range",
        "a move went past an axis limit: check the gcode or homing",
    ),
)


UNKNOWN_HINT = "see Klipper's log for the shutdown reason"
SHUTDOWN_HINTS = {cause: hint for _, cause, hint in SHUTDOWN_CAUSES}
MAINS_OFF_HINT = (
    "mains was off, so the board ran on USB backfeed and its link stalled; "
    "the next start powers it on and restarts Klipper"
)


def classify_shutdown(message: str) -> tuple[str, str]:
    low = message.lower()
    for needle, cause, hint in SHUTDOWN_CAUSES:
        if needle in low:
            return cause, hint
    return "unknown", UNKNOWN_HINT


def with_mains_off(ev: Event) -> Event:
    """A Klipper shutdown while the printer's power device reads off is expected, not a fault: info, no alert."""
    ev.severity = "info"
    ev.alert = False
    ev.data["mains"] = "off"
    cause = str(ev.data.get("cause") or "unknown").replace("_", " ")
    ev.message = f"Klipper shutdown with mains off ({cause}): {MAINS_OFF_HINT}"
    return ev


def watched_object(name: str) -> bool:
    """Objects the rules read beyond the fixed subscription: fans, runout sensors, temperature sensors."""
    return name == "fan" or name.startswith(
        FAN_PREFIXES[1:] + RUNOUT_PREFIXES + TEMP_PREFIXES
    )


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
        self.fan_since: dict[str, float] = {}
        self.fan_alerted: set[str] = set()
        self.runout_prev: dict[str, bool] = {}
        self.hot_alerted: set[str] = set()
        self.first_layer_sent = False

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
            msg = st.get("webhooks", {}).get("state_message", "") or ""
            cause, hint = classify_shutdown(msg)
            out.append(
                self._ev(
                    "klippy_shutdown",
                    "critical",
                    f"Klipper shutdown ({cause.replace('_', ' ')}): {hint}. {msg[:200]}",
                    cause=cause,
                    state_message=msg[:500],
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
                        filament_used_mm=ps.get("filament_used"),
                    )
                )
        self.prev_state = state

        out.extend(self._temperatures(st))
        if state == "printing":
            out.extend(self._heaters(st, now))
            out.extend(self._stall(st, now))
            out.extend(self._fans(st, now))
            out.extend(self._runout(st))
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
                check = self.rules.first_layer_check
                if check and check <= layer <= check + 2 and not self.first_layer_sent:
                    self.first_layer_sent = True
                    out.append(
                        self._ev(
                            "first_layer_check",
                            "info",
                            f"Layer {layer} reached: check the first layers",
                            alert=False,
                            layer=layer,
                            filename=fn,
                        )
                    )
        return out

    def _fans(self, st: dict[str, Any], now: float) -> list[Event]:
        """A fan commanded on whose tachometer reads ~0. Fans without a tachometer report rpm None and are skipped."""
        out = []
        for name, d in st.items():
            if not (
                name == "fan" or name.startswith(FAN_PREFIXES[1:])
            ) or not isinstance(d, dict):
                continue
            rpm, speed = d.get("rpm"), d.get("speed") or 0
            if rpm is None or speed < 0.3 or rpm >= self.rules.fan_min_rpm:
                self.fan_since.pop(name, None)
                self.fan_alerted.discard(name)
                continue
            since = self.fan_since.setdefault(name, now)
            if now - since > self.rules.fan_stall_s and name not in self.fan_alerted:
                self.fan_alerted.add(name)
                out.append(
                    self._ev(
                        "fan_stalled",
                        "critical",
                        f"{name} commanded {speed:.0%} but reads {rpm:.0f} rpm for {now - since:.0f}s",
                        fan=name,
                        speed=speed,
                        rpm=rpm,
                    )
                )
        return out

    def _runout(self, st: dict[str, Any]) -> list[Event]:
        out = []
        for name, d in st.items():
            if (
                not name.startswith(RUNOUT_PREFIXES)
                or not isinstance(d, dict)
                or not d.get("enabled", True)
            ):
                continue
            detected = d.get("filament_detected")
            if detected is None:
                continue
            if self.runout_prev.get(name) and not detected:
                out.append(
                    self._ev(
                        "filament_runout",
                        "warning",
                        f"{name.split(' ', 1)[-1]} lost filament (Klipper pauses if the sensor is set to)",
                        sensor=name,
                    )
                )
            self.runout_prev[name] = bool(detected)
        return out

    def _temperatures(self, st: dict[str, Any]) -> list[Event]:
        """Board/host sensors over BOARD_LIMIT_C, or any sensor over its rules.temp_limits entry; once until it cools."""
        out = []
        for name, d in st.items():
            if not name.startswith(TEMP_PREFIXES) or not isinstance(d, dict):
                continue
            temp = d.get("temperature")
            limit = self.rules.temp_limits.get(
                name, self.rules.temp_limits.get(name.split(" ", 1)[-1])
            )
            if limit is None and any(h in name.lower() for h in BOARD_HINTS):
                limit = BOARD_LIMIT_C
            if temp is None or limit is None:
                continue
            if temp > limit and name not in self.hot_alerted:
                self.hot_alerted.add(name)
                out.append(
                    self._ev(
                        "over_temperature",
                        "warning",
                        f"{name.split(' ', 1)[-1]} at {temp:.0f}°C, over its {limit:.0f}°C limit",
                        sensor=name,
                        temperature=temp,
                        limit=limit,
                    )
                )
            elif temp < limit - TEMP_HYSTERESIS_C:
                self.hot_alerted.discard(name)
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
        self.fan_since.clear()
        self.fan_alerted.clear()
        self.runout_prev.clear()
        self.first_layer_sent = False

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
