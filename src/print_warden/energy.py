"""Energy per print, from a Home Assistant sensor on the printer's plug.

A printer's `power_entity` is either an energy counter (unit kWh, Wh or MWh: the print's energy is the counter's rise
from start to end) or a power sensor (W or kW: sampled every `SAMPLE_S` while printing and integrated, trapezoid by
trapezoid). The reading when the first layer begins splits off the heat-up (bed, nozzle, homing, mesh). The meter's
state lives in the job store's kv table, so a warden restart mid-print keeps what was measured so far (a power sensor
loses the gap).

Cost is energy times a price: `energy.price_per_kwh`, or the mean of a price entity at start and end
(`energy.price_entity`, which defaults to `policy.cheap_power.entity`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

SAMPLE_S = 30.0
ENERGY_UNITS = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
POWER_UNITS = {"w": 1.0, "kw": 1000.0}
MAX_GAP_S = 300.0


@dataclass(frozen=True)
class Reading:
    kind: str  # "energy" (kWh) or "power" (W)
    value: float


def parse(state: dict[str, Any] | None) -> Reading | None:
    """A Home Assistant state as kWh (energy) or W (power), or None when it is unavailable or in another unit."""
    if not state:
        return None
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "")
    try:
        v = float(state.get("state"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    u = unit.strip().lower()
    if u in ENERGY_UNITS:
        return Reading("energy", v * ENERGY_UNITS[u])
    if u in POWER_UNITS:
        return Reading("power", v * POWER_UNITS[u])
    return None


def integrate(prev_t: float, prev_w: float, t: float, w: float) -> float:
    """kWh between two power samples; a gap longer than MAX_GAP_S (the warden was down) counts nothing."""
    dt = t - prev_t
    if dt <= 0 or dt > MAX_GAP_S:
        return 0.0
    return (prev_w + w) / 2 * dt / 3_600_000


class KV(Protocol):
    def kv_get(self, k: str, default: str | None = None) -> str | None: ...
    def kv_set(self, k: str, v: str) -> None: ...
    def kv_delete(self, k: str) -> bool: ...


Reader = Callable[[], Awaitable[dict[str, Any] | None]]


class Meter:
    """One printer's meter; `state` (in kv under energy:<job>) holds the running measurement of the current job."""

    def __init__(self, kv: KV, read: Reader):
        self.kv = kv
        self.read = read

    def _load(self, job_id: str) -> dict[str, Any] | None:
        raw = self.kv.kv_get(f"energy:{job_id}")
        return json.loads(raw) if raw else None

    def _save(self, job_id: str, st: dict[str, Any]) -> None:
        self.kv.kv_set(f"energy:{job_id}", json.dumps(st))

    async def start(
        self, job_id: str, price: float | None, now: float | None = None
    ) -> None:
        r = parse(await self.read())
        if r is None:
            return
        t = time.time() if now is None else now
        self._save(
            job_id,
            {"kind": r.kind, "start": r.value, "t": t, "last": r.value, "kwh": 0.0,
             "heatup_kwh": None, "price_start": price},
        )  # fmt: skip

    async def sample(self, job_id: str, now: float | None = None) -> None:
        """Integrate one power sample; an energy counter needs none."""
        st = self._load(job_id)
        if not st or st["kind"] != "power":
            return
        r = parse(await self.read())
        if r is None or r.kind != "power":
            return
        t = time.time() if now is None else now
        st["kwh"] += integrate(st["t"], st["last"], t, r.value)
        st["t"], st["last"] = t, r.value
        self._save(job_id, st)

    async def _so_far(self, st: dict[str, Any], now: float | None) -> float | None:
        if st["kind"] == "power":
            return float(st["kwh"])
        r = parse(await self.read())
        if r is None or r.kind != "energy":
            return None
        return max(0.0, r.value - st["start"])

    async def first_layer(self, job_id: str, now: float | None = None) -> None:
        st = self._load(job_id)
        if not st or st.get("heatup_kwh") is not None:
            return
        await self.sample(job_id, now)
        st = self._load(job_id) or st
        st["heatup_kwh"] = await self._so_far(st, now)
        self._save(job_id, st)

    async def stop(
        self, job_id: str, price: float | None, now: float | None = None
    ) -> dict[str, Any] | None:
        """The job's energy fields (energy_kwh, heatup_kwh, energy_cost), or None when it was not metered."""
        st = self._load(job_id)
        if not st:
            return None
        await self.sample(job_id, now)
        st = self._load(job_id) or st
        kwh = await self._so_far(st, now)
        self.kv.kv_delete(f"energy:{job_id}")
        if kwh is None:
            return None
        prices = [p for p in (st.get("price_start"), price) if p is not None]
        out: dict[str, Any] = {
            "energy_kwh": round(kwh, 4),
            "heatup_kwh": None
            if st.get("heatup_kwh") is None
            else round(st["heatup_kwh"], 4),
        }
        if prices:
            out["energy_cost"] = round(kwh * sum(prices) / len(prices), 3)
        return out


def average_kw(jobs: list[dict[str, Any]]) -> float | None:
    """Mean draw while printing, from metered finished jobs (kWh over hours printed)."""
    kwh = hours = 0.0
    for j in jobs:
        if j.get("energy_kwh") and j.get("print_duration_s"):
            kwh += float(j["energy_kwh"])
            hours += float(j["print_duration_s"]) / 3600
    return kwh / hours if hours else None
