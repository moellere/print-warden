"""The week's plan: when each queued job would start and finish on its printer, and what it waits for.

Each printer is a lane. It is free now, or when its current print ends plus `CLEAR_GAP_S` for someone to clear the
bed (an occupied bed adds the same gap). Its queued jobs follow in queue order, each at the first `STEP_S` slot
from the lane's free time that passes:
- the printer's `finish_window`: the estimated finish (with the printer's ETA correction) falls inside it;
- for a printer started by a tap, quiet hours: nobody is asked to tap at night;
- for an `auto_start` printer, its `auto_start_hours`.

Each job is followed by the bed-clearing gap. A job with no such slot in `HORIZON_S` is listed unplanned with the
reason.

With a price forecast (`energy.plan_by_price`), a job may wait past its first slot for cheaper power. The planner
searches up to `energy.plan_max_delay_hours` ahead for the slot with the lowest mean price over the print. It moves
the job only when that saves at least `MIN_SAVING` and the forecast covers the whole print. Every window still
applies. `parse_forecast` reads the hourly (or finer) prices that Home Assistant's price integrations keep in a
sensor's attributes: Nord Pool's `raw_today`/`raw_tomorrow`, lists of `{start, end, value}` under any name, or
`today`/`tomorrow` as plain lists from local midnight. The plan is pure and deterministic: the same queue, printers and clock give the same plan. With
`policy.plan_offers`, the queue holds a job's offer until its slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import policy

STEP_S = 15 * 60
MIN_SAVING = 0.10
_START_KEYS = (
    "start",
    "start_time",
    "startsAt",
    "starts_at",
    "from",
    "valid_from",
    "datetime",
    "time",
)
_END_KEYS = ("end", "end_time", "endsAt", "ends_at", "to", "valid_to")
_PRICE_KEYS = ("value", "price", "total", "value_inc_vat", "price_per_kwh", "cost")

Price = tuple[float, float, float]
"""(start, end, price per kWh)."""
CLEAR_GAP_S = 15 * 60
HORIZON_S = 7 * 86400


@dataclass(frozen=True)
class Lane:
    printer: str
    free_at: float
    """When the printer could start the next job, before the bed-clearing gap."""
    needs_clearing: bool
    """A print is running or the bed is occupied: the next start waits `CLEAR_GAP_S` more."""
    finish_window: str | None = None
    quiet_hours: str | None = None
    auto_start: bool = False
    auto_start_hours: str | None = None
    prices: tuple[Price, ...] = ()
    max_delay_s: float = 12 * 3600


def _local(t: float) -> datetime:
    return datetime.fromtimestamp(t).astimezone()


def _blocked(lane: Lane, t: float, est_s: float) -> str | None:
    """Why a start at `t` does not work, or None when it does."""
    at = _local(t)
    if lane.finish_window and not policy.finish_ok(lane.finish_window, at, est_s):
        return f"finishes outside {lane.finish_window}"
    if lane.auto_start:
        if lane.auto_start_hours and not policy.in_window(lane.auto_start_hours, at):
            return f"auto-start only {lane.auto_start_hours}"
    elif lane.quiet_hours and policy.in_window(lane.quiet_hours, at):
        return f"quiet hours {lane.quiet_hours}"
    return None


def _earliest(
    lane: Lane, ready: float, est_s: float
) -> tuple[float | None, str | None]:
    t = math.ceil(ready / STEP_S) * STEP_S if ready % STEP_S else ready
    first: str | None = None
    while t <= ready + HORIZON_S:
        why = _blocked(lane, t, est_s)
        if why is None:
            return t, first
        first = first or why
        t += STEP_S
    return None, first or "no slot"


def mean_price(prices: tuple[Price, ...], start: float, end: float) -> float | None:
    """The time-weighted mean price over [start, end], or None when the forecast does not cover it all."""
    if end <= start:
        return None
    covered = total = 0.0
    for a, b, p in prices:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            covered += hi - lo
            total += (hi - lo) * p
    return total / covered if covered >= (end - start) - 1 else None


def slot(lane: Lane, ready: float, est_s: float) -> tuple[float | None, str | None]:
    """(start, why it waits) for one job: the first step from `ready` that works (or, with prices, the cheapest one
    within `max_delay_s` of it when that saves `MIN_SAVING`), and the first reason it had to wait; (None, reason)
    when nothing in the horizon works."""
    t0, first = _earliest(lane, ready, est_s)
    if t0 is None or not lane.prices or est_s <= 0:
        return t0, first
    base = mean_price(lane.prices, t0, t0 + est_s)
    if base is None:
        return t0, first
    best, best_p = t0, base
    t = t0 + STEP_S
    while t <= t0 + lane.max_delay_s:
        if _blocked(lane, t, est_s) is None:
            p = mean_price(lane.prices, t, t + est_s)
            if p is not None and p < best_p:
                best, best_p = t, p
        t += STEP_S
    if best != t0 and best_p <= base * (1 - MIN_SAVING):
        return best, f"cheaper power (~{best_p:.3g} vs {base:.3g} a kWh)"
    return t0, first


def _when(v: Any) -> float | None:
    if isinstance(v, int | float):
        return float(v) / (1000 if v > 1e11 else 1)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v)
        except ValueError:
            return None
        return (d if d.tzinfo else d.astimezone()).timestamp()
    return None


def parse_forecast(attributes: dict[str, Any], now: float) -> tuple[Price, ...]:
    """Every future or current price interval a price sensor's attributes carry, sorted, without duplicates."""
    out: dict[float, Price] = {}

    def add(a: float | None, b: float | None, p: Any) -> None:
        try:
            price = float(p)
        except (TypeError, ValueError):
            return
        if a is not None and b is not None and b > a and b > now:
            out.setdefault(a, (a, b, price))

    for key, value in attributes.items():
        if not isinstance(value, list) or not value:
            continue
        if all(isinstance(v, dict) for v in value):
            rows = []
            for v in value:
                a = next((_when(v[k]) for k in _START_KEYS if k in v), None)
                b = next((_when(v[k]) for k in _END_KEYS if k in v), None)
                p = next((v[k] for k in _PRICE_KEYS if k in v), None)
                rows.append((a, b, p))
            rows.sort(key=lambda r: r[0] or 0)
            for i, (a, b, p) in enumerate(rows):
                if a is not None and b is None:
                    nxt = rows[i + 1][0] if i + 1 < len(rows) else None
                    b = nxt if nxt is not None and nxt > a else a + 3600
                add(a, b, p)
        elif key in ("today", "tomorrow") and all(
            isinstance(v, int | float) for v in value
        ):
            day = (
                datetime.fromtimestamp(now)
                .astimezone()
                .replace(hour=0, minute=0, second=0, microsecond=0)
            )
            base = day.timestamp() + (86400 if key == "tomorrow" else 0)
            step = 86400 / len(value)
            for i, p in enumerate(value):
                add(base + i * step, base + (i + 1) * step, p)
    return tuple(out[k] for k in sorted(out))


def plan(
    lanes: list[Lane], queues: dict[str, list[dict[str, Any]]], now: float
) -> dict[str, list[dict[str, Any]]]:
    """Per printer, its queued jobs (each with `id`, `name`, `est_s`) with `start`, `finish` and `waits`."""
    out: dict[str, list[dict[str, Any]]] = {}
    for lane in lanes:
        ready = max(now, lane.free_at) + (CLEAR_GAP_S if lane.needs_clearing else 0)
        rows = []
        for job in queues.get(lane.printer, []):
            est = float(job.get("est_s") or 0)
            if job.get("offered"):
                start: float | None = max(now, ready)
                why: str | None = "offered: waiting for the tap"
            else:
                start, why = slot(lane, max(now, ready), est)
            if start is None:
                rows.append(
                    {
                        "job_id": job["id"],
                        "name": job["name"],
                        "start": None,
                        "finish": None,
                        "waits": why,
                    }
                )
                continue
            if (
                why is None
                and lane.needs_clearing
                and start == ready
                and ready > now
                and not rows
            ):
                why = "the bed to be cleared"
            rows.append(
                {
                    "job_id": job["id"],
                    "name": job["name"],
                    "start": start,
                    "finish": start + est,
                    "waits": why,
                }
            )
            ready = start + est + CLEAR_GAP_S
        out[lane.printer] = rows
    return out


def describe(rows: list[dict[str, Any]], now: float) -> list[str]:
    """One line per job: `Tue 09:00 → 11:30 name (waits: …)`."""
    lines = []
    for r in rows:
        if r["start"] is None:
            lines.append(f"unplanned: {r['name']} ({r['waits']})")
            continue
        s, f = _local(r["start"]), _local(r["finish"])
        day = "" if s.date() == _local(now).date() else f"{s:%a} "
        line = f"{day}{s:%H:%M} → {f:%H:%M} {r['name']}"
        lines.append(line + (f" (waits: {r['waits']})" if r["waits"] else ""))
    return lines
