"""Scheduling decisions that are pure arithmetic: splitting copies across printers, and holding long prints for
cheap power.

Splitting: each candidate printer has a time until it is free, a (corrected) time per copy and how many copies fit on
its plate. Copies go one at a time to the printer that would finish its share soonest, so a busy printer takes fewer.
Nothing here starts anything: each share becomes an ordinary job, offered and gated like any other.

Batches: each job of a batch (a plate, or a model) has a time on every printer it fits. Longest first, each goes to
the printer that would finish it soonest given what that printer already has; the classic longest-processing-time
rule, within a third of the best possible finish.

Cheap power: an offer for a job expected to run at least `min_hours` waits while the price entity reads above
`max_price`. An unreadable price never holds a job; an explicit `request_start` is never held.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Candidate:
    printer: str
    free_in_s: float
    per_copy_s: float
    max_per_plate: int


def allocate(copies: int, cands: list[Candidate]) -> dict[str, int]:
    """Copies per printer, minimising when the last share finishes."""
    if copies < 1:
        raise ValueError("copies must be at least 1")
    room = sum(c.max_per_plate for c in cands)
    if copies > room:
        raise ValueError(
            f"{copies} copies do not fit: at most {room} across "
            + ", ".join(f"{c.printer} ({c.max_per_plate})" for c in cands)
        )
    share = {c.printer: 0 for c in cands}
    for _ in range(copies):
        best = min(
            (c for c in cands if share[c.printer] < c.max_per_plate),
            key=lambda c: (
                c.free_in_s + (share[c.printer] + 1) * c.per_copy_s,
                c.printer,
            ),
        )
        share[best.printer] += 1
    return {p: n for p, n in share.items() if n}


def finishes(share: dict[str, int], cands: list[Candidate]) -> dict[str, float]:
    by = {c.printer: c for c in cands}
    return {p: by[p].free_in_s + n * by[p].per_copy_s for p, n in share.items()}


def assign(
    durations: dict[str, dict[str, float]], free_in_s: dict[str, float]
) -> tuple[dict[str, str], dict[str, float]]:
    """Job key -> printer, and when each printer would finish its part. `durations[job][printer]` is the job's time
    on each printer it fits; a job with none raises."""
    load = dict(free_in_s)
    out: dict[str, str] = {}
    for job in sorted(
        durations, key=lambda j: (-max(durations[j].values(), default=0), j)
    ):
        fits = durations[job]
        if not fits:
            raise ValueError(f"{job} fits none of the printers")
        best = min(fits, key=lambda p: (load[p] + fits[p], p))
        load[best] += fits[best]
        out[job] = best
    return out, {p: load[p] for p in sorted(set(out.values()))}


def power_hold(
    est_s: float, price: str | None, max_price: float, min_hours: float
) -> str | None:
    """Why an offer should wait for cheaper power, or None."""
    if est_s < min_hours * 3600:
        return None
    try:
        now = float(price) if price is not None else None
    except ValueError:
        now = None
    if now is None or now <= max_price:
        return None
    return f"waiting for cheaper power: {now:g} > {max_price:g} for a {est_s / 3600:.1f} h print"


def plan_summary(
    share: dict[str, int], cands: list[Candidate], copies: int
) -> dict[str, Any]:
    done = finishes(share, cands)
    fastest_alone = min(c.free_in_s + copies * c.per_copy_s for c in cands)
    return {
        "shares": [
            {"printer": p, "copies": n, "finishes_in_s": round(done[p])}
            for p, n in sorted(share.items())
        ],
        "all_done_in_s": round(max(done.values())),
        "one_printer_would_take_s": round(fastest_alone),
    }
