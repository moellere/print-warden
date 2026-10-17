"""Home Assistant entities for each printer, pushed through HA's REST states API with the token the warden already
has (no MQTT broker needed). HA forgets REST-set states on restart, so the warden republishes periodically."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


def _slug(name: str) -> str:
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
) -> list[tuple[str, str, dict[str, Any]]]:
    """(entity_id, state, attributes) for one printer."""
    base = f"sensor.print_warden_{_slug(printer)}"
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
    return [
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
