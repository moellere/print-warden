"""Spool inventory: the physical side of Spoolman, which stays the system of record.

- **Register** a spool from the box: vendor, material, colour, net weight, empty-spool weight, price, where it is
  kept. The vendor and filament are matched (case-insensitive) or created; a new filament gets the material's usual
  density and 1.75 mm. The spool's short code (`S<id>`) is what goes on a label.
- **Weigh** it: remaining = gross − empty-spool weight (the spool's, else the filament's, else the vendor's), written
  to Spoolman. Against the last weigh-in (or the full spool), what the warden counted as used is compared with what
  the scale says was used; over `DRIFT` apart, with enough used to judge, the counting is off: a wrong density or
  diameter, or loads and purges done by hand.
- **Swap** a printer's spool: the outgoing one optionally weighed, the incoming one made active, and checked against
  the printer's queue (material, enough grams) and drying.
- **Retire** an empty spool (archived in Spoolman).

Weigh-in reminders come with upkeep: a loaded spool not weighed in `upkeep.weigh_every_days` gets a phone prompt that
takes the grams as a typed reply.
"""

from __future__ import annotations

import re
from typing import Any

DRIFT = 0.05
MIN_USED_G = 50.0
DENSITY = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PLA-CF": 1.30,
}
COLORS = {
    "black": "000000", "white": "FFFFFF", "grey": "808080", "gray": "808080", "silver": "C0C0C0",
    "red": "D32F2F", "orange": "F57C00", "yellow": "FBC02D", "green": "388E3C", "blue": "1976D2",
    "purple": "7B1FA2", "pink": "E91E63", "brown": "6D4C41", "natural": "F5F0E1", "clear": "EEEEEE",
}  # fmt: skip
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def code(spool_id: int) -> str:
    return f"S{spool_id}"


def parse_code(text: str | int) -> int:
    """A spool id from `12`, `S12` or `s12`."""
    t = str(text).strip().upper().removeprefix("S")
    if not t.isdigit():
        raise ValueError(f"{text!r} is not a spool code (S12) or id")
    return int(t)


def color(text: str) -> tuple[str, str]:
    """(hex without #, display name) from `#1976D2`, `1976d2` or a common colour name."""
    t = text.strip()
    if m := _HEX.match(t):
        return m[1].upper(), "#" + m[1].upper()
    if t.lower() in COLORS:
        return COLORS[t.lower()], t.capitalize()
    raise ValueError(
        f"colour {text!r}: give #RRGGBB or one of {', '.join(sorted(COLORS))}"
    )


def empty_weight(raw: dict[str, Any]) -> float | None:
    """The empty spool's weight from Spoolman: the spool's own, else its filament's, else the vendor's."""
    f = raw.get("filament") or {}
    for v in (
        raw.get("spool_weight"),
        f.get("spool_weight"),
        (f.get("vendor") or {}).get("empty_spool_weight"),
    ):
        if v:
            return float(v)
    return None


def drift(reference_g: float, counted_g: float, measured_g: float) -> dict[str, Any]:
    """Counted against weighed use since `reference_g` (the last weigh-in, or the full spool)."""
    used_counted = reference_g - counted_g
    used_measured = reference_g - measured_g
    out: dict[str, Any] = {
        "used_counted_g": round(used_counted, 1),
        "used_measured_g": round(used_measured, 1),
        "drift_pct": None,
        "flag": None,
    }
    if used_measured >= MIN_USED_G:
        pct = (used_counted - used_measured) / used_measured
        out["drift_pct"] = round(100 * pct, 1)
        if abs(pct) > DRIFT:
            direction = "under" if pct < 0 else "over"
            out["flag"] = (
                f"the warden counted {abs(pct):.0%} {direction} what the scale says was used: check the filament's "
                "density and diameter in Spoolman, or loads and purges done by hand"
            )
    return out


def swap_checks(
    spool: dict[str, Any],
    queue: list[dict[str, Any]],
    days_open: float | None,
    dry_after_days: float | None,
) -> list[str]:
    """What to know about a spool going onto a printer: the queue's materials and grams, and drying."""
    out = []
    mat = spool.get("material")
    other = sorted(
        {j["material"] for j in queue if j.get("material") and j["material"] != mat}
    )
    if other:
        out.append(f"the queue has {', '.join(other)} jobs; this spool is {mat}")
    need = sum(
        float(j.get("filament_g") or 0) for j in queue if j.get("material") == mat
    )
    have = spool.get("remaining_g")
    if have is not None and need > float(have):
        out.append(
            f"the queued {mat} jobs need {need:.0f} g; the spool has {float(have):.0f} g"
        )
    if dry_after_days and days_open is not None and days_open > dry_after_days:
        out.append(
            f"open {days_open:.0f} days (dry after {dry_after_days:g}): dry it first"
        )
    return out


def weigh_due(
    loaded: dict[str, dict[str, Any]],
    weighed_at: dict[int, float],
    opened_at: dict[int, float],
    every_days: float,
    now: float,
) -> list[dict[str, Any]]:
    """Loaded spools (printer -> spool) not weighed for `every_days` (since they were opened, if never)."""
    out = []
    for printer, s in sorted(loaded.items()):
        since = weighed_at.get(s["id"])
        if since is None:
            since = opened_at.get(s["id"])
        if since is None:
            continue
        days = (now - since) / 86400
        if days >= every_days:
            out.append(
                {
                    "printer": printer,
                    "spool_id": s["id"],
                    "code": code(s["id"]),
                    "name": s.get("name") or s.get("material"),
                    "days": round(days, 1),
                    "weighed": s["id"] in weighed_at,
                }
            )
    return out
