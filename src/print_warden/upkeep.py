"""Filament stock, drying and printer maintenance: what needs doing before it costs a print.

Pure functions over Spoolman spools, the job ledger and recorded resets; the server runs them hourly, sends each
finding to the phone at most once a day (outside quiet hours), and records a tap as the thing being done.

- Reorder: per material, what is left across Spoolman's spools minus what the queue still needs, below
  `upkeep.reorder_below_g`.
- Drying: a loaded spool of a material in `upkeep.dry_after_days`, open (first used) or last dried longer ago than
  that. "Dried" on the phone records it.
- Maintenance: per printer, `maintenance: {item: {every_hours: N} | {every_days: N}}`. Hours are the print time of
  finished jobs since the item was last done; days are calendar days. "Done" on the phone resets it. An item seen for
  the first time starts counting then, so a new install does not raise everything at once.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

QUEUED = ("sliced", "staged", "awaiting_approval", "approved")


@dataclass(frozen=True)
class Interval:
    every_hours: float | None = None
    every_days: float | None = None


def parse_maintenance(raw: dict[str, Any] | None) -> dict[str, Interval]:
    out: dict[str, Interval] = {}
    for item, spec in (raw or {}).items():
        hours, days = spec.get("every_hours"), spec.get("every_days")
        if hours is None and days is None:
            raise ValueError(f"maintenance.{item} needs every_hours or every_days")
        out[str(item)] = Interval(
            float(hours) if hours is not None else None,
            float(days) if days is not None else None,
        )
    return out


def reorder(
    spools: list[dict[str, Any]], jobs: list[dict[str, Any]], below_g: float
) -> list[dict[str, Any]]:
    """Materials whose stock, after the queued jobs, is under `below_g`."""
    have: dict[str, float] = defaultdict(float)
    need: dict[str, float] = defaultdict(float)
    for s in spools:
        if not s.get("archived") and s.get("material"):
            have[s["material"]] += float(s.get("remaining_g") or 0)
    for j in jobs:
        if j.get("state") in QUEUED and j.get("material"):
            need[j["material"]] += float(j.get("filament_g") or 0)
    out = []
    for m in sorted(set(have) | set(need)):
        left = have[m] - need[m]
        if left < below_g:
            out.append(
                {
                    "material": m,
                    "remaining_g": round(have[m]),
                    "queued_g": round(need[m]),
                    "after_queue_g": round(left),
                }
            )
    return out


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def drying(
    loaded: dict[str, dict[str, Any]],
    dried_at: dict[int, float],
    dry_after_days: dict[str, float],
    now: float,
) -> list[dict[str, Any]]:
    """Loaded spools (printer → spool) of a hygroscopic material that have been open too long since drying."""
    out = []
    for printer, s in sorted(loaded.items()):
        limit = dry_after_days.get(s.get("material") or "")
        if not limit:
            continue
        since = max(
            filter(None, (_ts(s.get("first_used")), dried_at.get(s["id"]))),
            default=None,
        )
        if since is None:
            continue
        days = (now - since) / 86400
        if days >= limit:
            out.append(
                {
                    "printer": printer,
                    "spool_id": s["id"],
                    "material": s["material"],
                    "name": s.get("name"),
                    "days_open": round(days, 1),
                    "dry_after_days": limit,
                }
            )
    return out


def maintenance(
    printer: str,
    items: dict[str, Interval],
    jobs: list[dict[str, Any]],
    resets: dict[str, float],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Per item: hours printed and days since it was last done, the share of its interval used, and whether it is due."""
    now = now or time.time()
    out = []
    for item, every in sorted(items.items()):
        since = resets.get(item, now)
        hours = sum(
            float(j.get("print_duration_s") or 0) / 3600
            for j in jobs
            if j.get("printer") == printer
            and j.get("state") in ("done", "failed")
            and (j.get("finished_at") or 0) > since
        )
        days = (now - since) / 86400
        used = max(
            hours / every.every_hours if every.every_hours else 0,
            days / every.every_days if every.every_days else 0,
        )
        out.append(
            {
                "printer": printer,
                "item": item,
                "hours": round(hours, 1),
                "days": round(days, 1),
                "every_hours": every.every_hours,
                "every_days": every.every_days,
                "used_pct": round(100 * used),
                "due": used >= 1,
            }
        )
    return out
