"""Home Assistant entities for each printer, pushed through HA's REST states API with the token the warden already
has (no MQTT broker needed). HA forgets REST-set states on restart, so the warden republishes periodically. The same
rows feed MQTT discovery (`mqtt_entities.py`) when a broker is configured."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

Row = tuple[str, str, dict[str, Any]]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def printer_entities(
    printer: str,
    status: dict[str, Any],
    job: dict[str, Any] | None,
    left_s: float | None,
    queue_len: int,
    bed_state: str,
    material: str | None,
    now: datetime,
    maintenance: list[dict[str, Any]] | None = None,
) -> list[Row]:
    """(entity_id, state, attributes) for one printer; one `maint_<item>` sensor (share of its interval used) per
    configured maintenance item."""
    base = f"sensor.print_warden_{slug(printer)}"
    state = (
        "offline"
        if not status.get("moonraker_connected")
        else status.get("state") or status.get("klippy") or "unknown"
    )
    printing = state in ("printing", "paused")
    finish = (
        (now + timedelta(seconds=left_s)).isoformat()
        if printing and left_s is not None
        else "unknown"
    )
    common = {"printer": printer}
    rows: list[Row] = [
        (
            f"{base}_state",
            state,
            {
                **common,
                "friendly_name": f"{printer} state",
                "icon": "mdi:printer-3d",
                "klippy": status.get("klippy"),
                "job": job["name"] if job and printing else None,
                "job_id": job["id"] if job and printing else None,
                "filename": status.get("filename"),
                "layer": status.get("layer"),
                "total_layers": status.get("total_layers"),
            },
        ),
        (
            f"{base}_progress",
            str(status.get("progress_pct") or 0) if printing else "0",
            {
                **common,
                "friendly_name": f"{printer} progress",
                "unit_of_measurement": "%",
                "icon": "mdi:progress-clock",
            },
        ),
        (
            f"{base}_finish",
            finish,
            {
                **common,
                "friendly_name": f"{printer} finishes",
                "device_class": "timestamp",
            },
        ),
        (
            f"{base}_queue",
            str(queue_len),
            {
                **common,
                "friendly_name": f"{printer} queue",
                "unit_of_measurement": "jobs",
                "icon": "mdi:tray-full",
            },
        ),
        (
            f"{base}_bed",
            bed_state,
            {**common, "friendly_name": f"{printer} bed", "icon": "mdi:texture-box"},
        ),
        (
            f"{base}_material",
            material or "unknown",
            {
                **common,
                "friendly_name": f"{printer} material",
                "icon": "mdi:printer-3d-nozzle",
            },
        ),
    ]
    rows += [
        (
            f"{base}_maint_{slug(m['item'])}",
            str(m["used_pct"]),
            {
                **common,
                "friendly_name": f"{printer} {m['item']} maintenance",
                "unit_of_measurement": "%",
                "icon": "mdi:wrench-clock",
                "hours": m["hours"],
                "days": m["days"],
                "every_hours": m["every_hours"],
                "every_days": m["every_days"],
                "due": m["due"],
            },
        )
        for m in maintenance or []
    ]
    return rows
