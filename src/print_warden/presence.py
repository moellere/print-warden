"""Away mode: what changes while nobody is home, from a Home Assistant presence entity.

`presence.entity` is a `person.*`, a group, or `zone.home` (its state is how many people are in it); home is a
state in `home_states` (default `home`) or a count above zero. The warden reads it every `poll_s` and keeps only the
current answer and when the house emptied, in memory: presence is never written to disk or the event log.

While away (unknown counts as away for auto-start only):
- no unattended start, unless `auto_start_when_away`;
- with `away_offer_max_hours`, a longer job is not offered (a tap can still come from anywhere, but nobody is there
  to clear the bed or see it fail);
- quiet hours stop muting alerts: a warning at night matters more when nobody is in the house.

Coming home sends one "while you were out" summary: what finished, failed or cancelled itself, and what waits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATE: dict[str, Any] = {"home": None, "away_since": None}


@dataclass(frozen=True)
class Presence:
    entity: str
    home_states: tuple[str, ...] = ("home",)
    auto_start_when_away: bool = False
    away_offer_max_hours: float | None = None
    poll_s: float = 60.0


def is_home(state: str | None, home_states: tuple[str, ...]) -> bool | None:
    """True / False from the entity's state; None when it is unreadable or unavailable."""
    if state is None or state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state) > 0
    except ValueError:
        return state in home_states


def update(home: bool | None, now: float) -> str | None:
    """Record a reading. Returns "left" or "back" on a change between known states, else None."""
    prev = STATE["home"]
    if home is None:
        return None
    STATE["home"] = home
    if prev is True and home is False:
        STATE["away_since"] = now
        return "left"
    if prev is False and home is True:
        return "back"
    return None


def away() -> bool:
    """Known to be away (unknown is not away, so alerts keep their quiet hours)."""
    return STATE["home"] is False


def reset() -> None:
    STATE.update(home=None, away_since=None)


def summary(
    jobs: list[dict[str, Any]], printers: dict[str, dict[str, Any]], hours: float
) -> str:
    """What happened while away: the jobs that ended (newest first), then each printer's bed and queue."""
    ended = [j for j in jobs if j.get("state") in ("done", "failed")]
    lines = [f"Away {hours:.1f} h."]
    if ended:
        done = [j["name"] for j in ended if j["state"] == "done"]
        failed = [
            f"{j['name']} ({(j.get('failure_cause') or j.get('error') or 'failed').split(':')[0].replace('_', ' ')})"
            for j in ended
            if j["state"] == "failed"
        ]
        if done:
            lines.append("Finished: " + ", ".join(done) + ".")
        if failed:
            lines.append("Failed: " + ", ".join(failed) + ".")
    else:
        lines.append("No prints ended.")
    for name, p in sorted(printers.items()):
        queued = p.get("queued") or 0
        lines.append(
            f"{name}: bed {p.get('bed') or 'unknown'}"
            + (f", {queued} queued" if queued else "")
            + (", waiting for a Start tap" if p.get("awaiting") else "")
            + "."
        )
    return " ".join(lines)
