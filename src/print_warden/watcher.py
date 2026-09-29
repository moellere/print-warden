"""Per-printer orchestration: Moonraker updates -> rules -> events, snapshots, alerts, light, timelapse frames."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import cameras
from .config import Config, Printer
from .events import Event, EventStore
from .ha import HAClient
from .jobs import JobStore
from .moonraker import MoonrakerClient
from .rules import RuleEngine

log = logging.getLogger(__name__)
ALERT_COOLDOWN_S = 300


class PrinterWatcher:
    def __init__(
        self,
        cfg: Config,
        printer: Printer,
        store: EventStore,
        ha: HAClient | None,
        jobs: JobStore | None = None,
    ):
        self.jobs = jobs
        self.cfg = cfg
        self.p = printer
        self.store = store
        self.ha = ha
        self.rules = RuleEngine(printer.name, printer.rules)
        self.mr = MoonrakerClient(printer.ws_url, self._on_update)
        self.light_prior: str | None = None
        self.job_dir: Path | None = None
        self.last_alert: dict[str, float] = {}
        self.last_update = 0.0

    async def run(self) -> None:
        await self.mr.run()

    async def _on_update(self, state: dict[str, Any], signal: str | None) -> None:
        self.last_update = time.time()
        for ev in self.rules.evaluate(state, signal):
            await self.handle(ev)

    async def handle(self, ev: Event, snapshot_camera: str | None = None) -> None:
        cam = snapshot_camera or (
            "bed" if "bed" in self.p.cameras else next(iter(self.p.cameras), None)
        )
        if ev.kind == "print_started":
            await self._light(True)
            self.job_dir = (
                self.cfg.storage_dir
                / "timelapse"
                / self.p.name
                / time.strftime("%Y%m%d-%H%M%S")
            )
        if (
            ev.kind == "layer_changed"
            and self.p.rules.layer_snapshot
            and self.job_dir
            and cam
        ):
            await self._snap(cam, self.job_dir, f"L{ev.data.get('layer', 0):05d}")
        if ev.alert and cam:
            path = await self._snap(cam, None, None)
            if path:
                ev.snapshot = str(path.relative_to(self.cfg.storage_dir))
        self.store.add(ev)
        self._track_job(ev)
        if ev.kind in ("print_complete", "print_cancelled", "print_error"):
            await self._light(False)
            self.job_dir = None
        if ev.alert:
            await self._alert(ev)

    def _track_job(self, ev: Event) -> None:
        if not self.jobs:
            return
        fn = (ev.data.get("filename") or "").split("/")[-1]
        if ev.kind == "print_started":
            for j in self.jobs.list(
                self.p.name, 5, ("approved", "staged", "awaiting_approval")
            ):
                if (j["remote_name"] or "").split("/")[-1] == fn:
                    self.jobs.transition(
                        j["id"],
                        "printing",
                        "print_started seen",
                        started_at=time.time(),
                    )
                    break
        elif ev.kind in ("print_complete", "print_cancelled", "print_error"):
            active = self.jobs.find_active(self.p.name)
            if active and (
                not fn or (active["remote_name"] or "").split("/")[-1] == fn
            ):
                state = "done" if ev.kind == "print_complete" else "failed"
                self.jobs.transition(
                    active["id"],
                    state,
                    ev.kind,
                    finished_at=time.time(),
                    error=ev.message if state == "failed" else None,
                )

    async def _snap(self, cam: str, into: Path | None, name: str | None) -> Path | None:
        try:
            data = await cameras.fetch(
                self.p.cameras[cam],
                self.p.rotate.get(cam, 0),
                flip_v=self.p.flip_v.get(cam, False),
            )
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.warning("%s snapshot %s failed: %s", self.p.name, cam, e)
            return None
        if into is not None:
            into.mkdir(parents=True, exist_ok=True)
            path = into / f"{name}.jpg"
            path.write_bytes(data)
            return path
        return cameras.save(self.cfg.storage_dir, self.p.name, cam, data)

    async def _alert(self, ev: Event) -> None:
        if not self.ha:
            return
        now = time.time()
        if (
            ev.severity != "critical"
            and now - self.last_alert.get(ev.kind, 0) < ALERT_COOLDOWN_S
        ):
            return
        self.last_alert[ev.kind] = now
        url = f"{self.cfg.public_url}/files/{ev.snapshot}" if ev.snapshot else None
        await self.ha.alert(
            self.p.name, ev.severity, ev.kind.replace("_", " "), ev.message, url
        )

    async def _light(self, on: bool) -> None:
        if not (self.ha and self.p.light):
            return
        if on:
            self.light_prior = await self.ha.state(self.p.light)
            await self.ha.light(self.p.light, True)
        elif self.light_prior == "off":
            await asyncio.sleep(2)
            await self.ha.light(self.p.light, False)

    def status(self) -> dict[str, Any]:
        st = self.mr.state
        ps = st.get("print_stats", {})
        return {
            "printer": self.p.name,
            "moonraker_connected": self.mr.connected,
            "klippy": st.get("webhooks", {}).get("state"),
            "klippy_message": st.get("webhooks", {}).get("state_message"),
            "state": ps.get("state"),
            "filename": ps.get("filename"),
            "progress_pct": round(
                100 * float(st.get("virtual_sdcard", {}).get("progress") or 0), 1
            ),
            "print_duration_min": round(float(ps.get("print_duration") or 0) / 60, 1),
            "layer": (ps.get("info") or {}).get("current_layer"),
            "total_layers": (ps.get("info") or {}).get("total_layer"),
            "extruder": {
                k: st.get("extruder", {}).get(k) for k in ("temperature", "target")
            },
            "bed": {
                k: st.get("heater_bed", {}).get(k) for k in ("temperature", "target")
            },
            "last_update_s_ago": round(time.time() - self.last_update)
            if self.last_update
            else None,
            "timelapse_frames": len(list(self.job_dir.glob("*.jpg")))
            if self.job_dir and self.job_dir.exists()
            else 0,
        }
