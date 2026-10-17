"""History and insight over the job ledger: how good Orca's estimates are on each printer, how often prints fail and
by what, and what printing used per month. Pure functions over job rows; only finished jobs (done/failed) count."""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Any

FINISHED = ("done", "failed")
CORRECTION_MIN_SAMPLES = 3
CORRECTION_WINDOW = 20
CORRECTION_CLAMP = (0.5, 2.0)


def _ratio(job: dict[str, Any]) -> float | None:
    est, actual = job.get("est_seconds"), job.get("print_duration_s")
    if job.get("state") != "done" or not est or not actual:
        return None
    return float(actual) / float(est)


def estimate_accuracy(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (printer, quality): how long completed prints really took versus Orca's estimate."""
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for j in jobs:
        if (r := _ratio(j)) is not None:
            groups[(j["printer"], j.get("quality") or "?")].append(r)
    return [
        {
            "printer": p,
            "quality": q,
            "prints": len(rs),
            "median_actual_over_estimate": round(statistics.median(rs), 3),
            "worst": round(max(rs), 3),
        }
        for (p, q), rs in sorted(groups.items())
    ]


def correction(jobs: list[dict[str, Any]], printer: str) -> float:
    """Factor to apply to Orca's estimate on this printer: the median ratio of its recent completed prints, clamped;
    1.0 until there are enough of them."""
    rs = [
        r
        for j in reversed(jobs)
        if j["printer"] == printer and (r := _ratio(j)) is not None
    ]
    rs = rs[:CORRECTION_WINDOW]
    if len(rs) < CORRECTION_MIN_SAMPLES:
        return 1.0
    lo, hi = CORRECTION_CLAMP
    return round(min(hi, max(lo, statistics.median(rs))), 3)


def outcomes(jobs: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Done vs failed grouped by `key` (printer, material or quality), with the commonest failure reasons."""
    done: dict[str, int] = defaultdict(int)
    failed: dict[str, int] = defaultdict(int)
    reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for j in jobs:
        if j.get("state") not in FINISHED:
            continue
        k = str(j.get(key) or "?")
        if j["state"] == "done":
            done[k] += 1
        else:
            failed[k] += 1
            reasons[k][(j.get("error") or "unknown").split(":")[0][:60]] += 1
    out = []
    for k in sorted(set(done) | set(failed)):
        n = done[k] + failed[k]
        top = sorted(reasons[k].items(), key=lambda kv: -kv[1])[:3]
        out.append(
            {
                key: k,
                "prints": n,
                "done": done[k],
                "failed": failed[k],
                "failure_rate": round(failed[k] / n, 3),
                "top_failures": [{"reason": r, "count": c} for r, c in top],
            }
        )
    return out


def monthly(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per calendar month (local time): prints, hours, grams and cost of finished jobs."""
    rows: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for j in jobs:
        if j.get("state") not in FINISHED:
            continue
        month = time.strftime(
            "%Y-%m", time.localtime(j.get("finished_at") or j["created"])
        )
        r = rows[month]
        r["prints"] += 1
        r["failed"] += j["state"] == "failed"
        r["hours"] += (j.get("print_duration_s") or 0) / 3600
        r["grams"] += j.get("grams_used") or 0
        r["cost"] += j.get("cost") or 0
    return [
        {
            "month": m,
            "prints": int(r["prints"]),
            "failed": int(r["failed"]),
            "hours": round(r["hours"], 1),
            "grams": round(r["grams"]),
            "cost": round(r["cost"], 2),
        }
        for m, r in sorted(rows.items())
    ]
