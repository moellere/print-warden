"""The weekly digest: what the printers did in the last seven days and what is due, from rows already recorded.

Counted: finished prints (done and failed; a human's label wins over how the job ended), the commonest failure
reasons, print hours, grams, filament cost, kWh and energy cost, per printer and in all. Fault tests are left out.
Due: maintenance past its interval, spools to dry, spools to weigh, materials to reorder and materials the run-out
forecast gives under `RUNS_OUT_DAYS`. Nothing here is generated; the same rows give the same digest.

`digest: {day: sunday, at: "18:00"}` sends it once a week, at the first hourly pass after that local time. While
nobody is home (away mode) it waits and is sent on return.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from . import calibration, outcome

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RUNS_OUT_DAYS = 21
WEEK_S = 7 * 86400


def day_index(day: str) -> int:
    d = day.strip().lower()
    for i, name in enumerate(DAYS):
        if name.startswith(d[:3]) and len(d) >= 3:
            return i
    raise ValueError(f"digest.day {day!r}: one of {', '.join(DAYS)}")


def slot(day: int, at: str, now: datetime) -> datetime:
    """The most recent scheduled moment at or before `now` (local time)."""
    hh, _, mm = at.partition(":")
    t = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    t -= timedelta(days=(now.weekday() - day) % 7)
    if t > now:
        t -= timedelta(days=7)
    return t


def summary(
    jobs: list[dict[str, Any]], upkeep: dict[str, Any], now: float
) -> dict[str, Any]:
    """The digest's numbers. `jobs` are the jobs of the last seven days; `upkeep` is the warden's upkeep report."""
    finished = [
        j
        for j in jobs
        if j.get("state") in ("done", "failed")
        and now - (j.get("finished_at") or j.get("created") or 0) <= WEEK_S
        and not calibration.is_fault(j)
    ]
    per: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sums = {"hours": 0.0, "grams": 0.0, "cost": 0.0, "kwh": 0.0, "energy_cost": 0.0}
    for j in finished:
        failed = outcome.failed(j)
        c = per.setdefault(j["printer"], Counter())
        c["prints"] += 1
        c["failed"] += failed
        totals["prints"] += 1
        totals["failed"] += failed
        if failed:
            reasons[outcome.reason(j)] += 1
        sums["hours"] += (j.get("print_duration_s") or 0) / 3600
        sums["grams"] += j.get("grams_used") or 0
        sums["cost"] += j.get("cost") or 0
        sums["kwh"] += j.get("energy_kwh") or 0
        sums["energy_cost"] += j.get("energy_cost") or 0
    return {
        "prints": totals["prints"],
        "failed": totals["failed"],
        "by_printer": {
            p: {"prints": c["prints"], "failed": c["failed"]}
            for p, c in sorted(per.items())
        },
        "reasons": [{"reason": r, "count": n} for r, n in reasons.most_common(3)],
        "hours": round(sums["hours"], 1),
        "grams": round(sums["grams"]),
        "cost": round(sums["cost"], 2),
        "kwh": round(sums["kwh"], 2),
        "energy_cost": round(sums["energy_cost"], 2),
        "due": {
            "maintenance": [
                f"{m['printer']} {m['item']}"
                for m in upkeep.get("maintenance") or []
                if m.get("due")
            ],
            "drying": [
                f"{d['printer']}: {d.get('name') or d['material']}"
                for d in upkeep.get("drying") or []
            ],
            "weigh": [
                f"{w['printer']}: {w['code']}" for w in upkeep.get("weigh") or []
            ],
            "reorder": [r["material"] for r in upkeep.get("reorder") or []],
            "running_out": [
                f"{f['material']} (~{f['days_left']:.0f} d)"
                for f in upkeep.get("forecast") or []
                if f.get("days_left") is not None and f["days_left"] < RUNS_OUT_DAYS
            ],
        },
    }


def message(d: dict[str, Any]) -> tuple[str, str]:
    """(title, text) for the phone."""
    if d["prints"]:
        why = ", ".join(
            f"{r['reason'].replace('_', ' ')} ×{r['count']}" for r in d["reasons"]
        )
        cost = d["cost"] + d["energy_cost"]
        parts = [
            f"{d['prints']} print(s), {d['failed']} failed"
            + (f" ({why})" if why else "")
            + f"; {d['hours']:g} h, {d['grams']} g"
            + (f", {d['kwh']:g} kWh" if d["kwh"] else "")
            + (f", cost {cost:.2f}" if cost else "")
            + ".",
            " ".join(
                f"{p}: {c['prints']} print(s), {c['failed']} failed."
                for p, c in d["by_printer"].items()
            ),
        ]
    else:
        parts = ["No prints finished this week."]
    labels = {
        "maintenance": "Maintenance due",
        "drying": "Dry",
        "weigh": "Weigh",
        "reorder": "Reorder",
        "running_out": "Running out",
    }
    due = [f"{labels[k]}: {', '.join(v)}." for k, v in d["due"].items() if v]
    parts.append(" ".join(due) if due else "Nothing due.")
    return "Week in prints", " ".join(p for p in parts if p)
