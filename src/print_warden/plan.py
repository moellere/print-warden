"""The week's plan: when each queued job would start and finish on its printer, and what it waits for.

Each printer is a lane. It is free now, or when its current print ends plus `CLEAR_GAP_S` for someone to clear the
bed (an occupied bed adds the same gap). Its queued jobs follow in queue order, each at the first `STEP_S` slot
from the lane's free time that passes:
- the printer's `finish_window`: the estimated finish (with the printer's ETA correction) falls inside it;
- for a printer started by a tap, quiet hours: nobody is asked to tap at night;
- for an `auto_start` printer, its `auto_start_hours`.

Each job is followed by the bed-clearing gap. A job with no such slot in `HORIZON_S` is listed unplanned with the
reason. The plan is pure and deterministic: the same queue, printers and clock give the same plan. With
`policy.plan_offers`, the queue holds a job's offer until its slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import policy

STEP_S = 15 * 60
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


def slot(lane: Lane, ready: float, est_s: float) -> tuple[float | None, str | None]:
    """(start, why it waits) for one job: the first step from `ready` that works, and the first reason it had to
    wait; (None, reason) when nothing in the horizon works."""
    t = math.ceil(ready / STEP_S) * STEP_S if ready % STEP_S else ready
    first: str | None = None
    while t <= ready + HORIZON_S:
        why = _blocked(lane, t, est_s)
        if why is None:
            return t, first
        first = first or why
        t += STEP_S
    return None, first or "no slot"


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
