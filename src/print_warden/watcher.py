"""Per-printer orchestration: Moonraker updates -> rules -> events, snapshots, alerts, light, timelapse, job
tracking, spool usage, and job reconciliation whenever the Moonraker connection (re)starts."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import bedcheck, cameras, moonraker, reconcile, timelapse
from .bedcheck import BedStore, Check
from .config import Config, Printer
from .events import Event, EventStore
from .ha import HAClient
from .jobs import JobStore
from .moonraker import MoonrakerClient
from .rules import RuleEngine
from .spoolman import Spoolman

log = logging.getLogger(__name__)
ALERT_COOLDOWN_S = 300
FINAL_KINDS = ("print_complete", "print_cancelled", "print_error")
BED_SETTLE_S = 30


def _hm(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m"


def used_grams(job: dict[str, Any], used_mm: float | None) -> float | None:
    """Grams actually extruded, scaled from the slicer's estimate by the length Klipper reports."""
    est_g, est_m = job.get("filament_g"), job.get("filament_m")
    if used_mm is not None and est_g and est_m:
        return float(est_g) * used_mm / (float(est_m) * 1000)
    return None


def completion_summary(
    job: dict[str, Any], duration_s: float, used_mm: float | None
) -> str:
    parts = [f"{job['name']} done in {_hm(duration_s)}"]
    est = job.get("est_seconds")
    if est and duration_s:
        parts.append(f"est {_hm(est)} ({duration_s / est - 1:+.0%})")
    grams = used_grams(job, used_mm)
    if grams is not None:
        parts.append(f"{grams:.0f} g")
    return ", ".join(parts)


class PrinterWatcher:
    def __init__(
        self,
        cfg: Config,
        printer: Printer,
        store: EventStore,
        ha: HAClient | None,
        jobs: JobStore | None = None,
        spoolman: Spoolman | None = None,
        bed: BedStore | None = None,
    ):
        self.jobs = jobs
        self.spoolman = spoolman
        self.bed = bed
        self.start_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
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
        if signal == "snapshot":
            await self.reconcile_jobs()

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def handle(self, ev: Event, snapshot_camera: str | None = None) -> None:
        cam = snapshot_camera or (
            "bed" if "bed" in self.p.cameras else next(iter(self.p.cameras), None)
        )
        if ev.kind == "print_started":
            if self.bed:
                self.bed.set_state(
                    self.p.name, "occupied", f"printing {ev.data.get('filename') or ''}"
                )
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
        try:
            job = self._track_job(ev)
        except ValueError as e:
            log.warning("%s job tracking: %s", self.p.name, e)
            job = None
        frames = self.job_dir
        deferred = False
        if ev.kind in FINAL_KINDS:
            await self._light(False)
            self.job_dir = None
            if self.bed:
                self.bed.set_state(self.p.name, "occupied", ev.kind)
                self._spawn(self._after_print())
            used = ev.data.get("filament_used_mm")
            if job:
                await self.settle_spool(job, used)
                if ev.kind == "print_complete":
                    ev.message = completion_summary(
                        job, ev.data.get("print_duration") or 0, used
                    )
            if frames and any(frames.glob("*.jpg")):
                deferred = ev.alert and ev.kind == "print_complete"
                self._spawn(
                    self._finish_timelapse(frames, job, ev if deferred else None)
                )
        self.store.add(ev)
        if ev.alert and not deferred:
            await self._alert(ev)

    def _track_job(self, ev: Event) -> dict[str, Any] | None:
        if not self.jobs:
            return None
        fn = (ev.data.get("filename") or "").split("/")[-1]
        if ev.kind == "print_started":
            for j in self.jobs.list(
                self.p.name, 5, ("approved", "staged", "awaiting_approval")
            ):
                if (j["remote_name"] or "").split("/")[-1] == fn:
                    return self.jobs.transition(
                        j["id"],
                        "printing",
                        "print_started seen",
                        started_at=time.time(),
                    )
        elif ev.kind in FINAL_KINDS:
            active = self.jobs.find_active(self.p.name)
            if active and (
                not fn or (active["remote_name"] or "").split("/")[-1] == fn
            ):
                state = "done" if ev.kind == "print_complete" else "failed"
                return self.jobs.transition(
                    active["id"],
                    state,
                    ev.kind,
                    finished_at=time.time(),
                    error=ev.message if state == "failed" else None,
                )
        return None

    async def settle_spool(self, job: dict[str, Any], used_mm: float | None) -> None:
        """Record what the print consumed and deduct it from the job's spool. Never raises."""
        if used_mm is None or not self.jobs:
            return
        sid = job.get("spool_id")
        if not (self.spoolman and self.p.track_spool_usage and sid):
            self.jobs.note(
                job["id"], f"used {used_mm:.0f} mm", filament_used_mm=used_mm
            )
            return
        try:
            spool = await self.spoolman.use_length(int(sid), used_mm)
            note = f"spool {sid}: -{used_mm:.0f} mm, {spool.get('remaining_g') or 0:.0f} g left"
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("%s spool %s deduction failed: %s", self.p.name, sid, e)
            note = f"spool {sid} deduction of {used_mm:.0f} mm failed: {e}"
        self.jobs.note(job["id"], note, filament_used_mm=used_mm)

    async def _finish_timelapse(
        self, frames: Path, job: dict[str, Any] | None, deferred: Event | None
    ) -> None:
        """Render the timelapse; a completion alert held back for it goes out afterwards, with or without the link."""
        out = await timelapse.render(frames)
        if out:
            rel = str(out.relative_to(self.cfg.storage_dir))
            url = f"{self.cfg.public_url}/files/{rel}"
            if job and self.jobs:
                self.jobs.note(job["id"], "timelapse rendered", timelapse=str(out))
            self.store.add(
                Event(
                    self.p.name,
                    "timelapse_ready",
                    "info",
                    f"Timelapse: {url}",
                    {"url": url},
                )
            )
            if deferred:
                deferred.message += f" — timelapse {url}"
        if deferred:
            await self._alert(deferred)

    async def reconcile_jobs(self) -> None:
        """Settle jobs whose outcome happened while the warden was down or disconnected. A start in flight holds
        start_lock, so pending jobs are left alone then."""
        if not self.jobs:
            return
        st = self.status()
        states = (
            ("printing",)
            if self.start_lock.locked()
            else ("printing", *reconcile.PENDING)
        )
        jobs = self.jobs.list(self.p.name, 20, states)
        history: list[dict[str, Any]] | None = []
        if any(j["state"] == "printing" for j in jobs):
            try:
                history = await moonraker.history(self.p.moonraker)
            except httpx.HTTPError as e:
                log.warning(
                    "%s history unavailable, printing jobs left as is: %s",
                    self.p.name,
                    e,
                )
                history = None
        for j in jobs:
            if j["state"] == "printing" and history is None:
                continue
            verdict = reconcile.decide(j, st, history or [])
            if not verdict:
                continue
            state, note, used = verdict
            fields: dict[str, Any] = {}
            if state == "printing":
                fields["started_at"] = time.time()
            elif state in ("done", "failed"):
                fields["finished_at"] = time.time()
                if state == "failed":
                    fields["error"] = note
            job = self.jobs.transition(j["id"], state, note, **fields)
            if self.bed and state in ("printing", "done", "failed"):
                self.bed.set_state(self.p.name, "occupied", note)
            if state in ("done", "failed"):
                await self.settle_spool(job, used)
                await self.handle(
                    Event(
                        self.p.name,
                        "job_reconciled",
                        "info" if state == "done" else "warning",
                        f"{job['name']}: {note}",
                        {"job_id": job["id"], "state": state},
                        alert=True,
                    )
                )

    @property
    def reference_path(self) -> Path | None:
        bc = self.p.bed_check
        if not bc:
            return None
        return self.cfg.storage_dir / "reference" / f"{self.p.name}_{bc.camera}.jpg"

    async def _lit_frame(self, cam: str) -> bytes:
        """A frame with the work light on, as the reference was taken; the light is put back as it was."""
        prior = None
        if self.ha and self.p.light:
            prior = await self.ha.state(self.p.light)
            if prior != "on":
                await self.ha.light(self.p.light, True)
                await asyncio.sleep(2)
        try:
            return await cameras.fetch(
                self.p.cameras[cam],
                self.p.rotate.get(cam, 0),
                flip_v=self.p.flip_v.get(cam, False),
            )
        finally:
            if self.ha and self.p.light and prior == "off":
                await self.ha.light(self.p.light, False)

    async def capture_reference(self) -> Path:
        bc, ref = self.p.bed_check, self.reference_path
        if not bc or not ref:
            raise ValueError(f"{self.p.name} has no bed_check configured")
        if bc.camera not in self.p.cameras:
            raise ValueError(
                f"bed_check camera {bc.camera!r} is not one of {sorted(self.p.cameras)}"
            )
        data = await self._lit_frame(bc.camera)
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(data)
        return ref

    async def check_bed(self, label: str | None = None) -> Check | None:
        """Compare the bed camera to the empty-bed reference. `label` is what a human knows the bed to be right
        now; labelled checks are the record auto_start is judged on. None when there is nothing to compare."""
        bc, ref = self.p.bed_check, self.reference_path
        if not (bc and ref and ref.exists() and bc.camera in self.p.cameras):
            return None
        try:
            data = await self._lit_frame(bc.camera)
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.warning("%s bed check: camera failed: %s", self.p.name, e)
            return None
        snap = cameras.save(
            self.cfg.storage_dir, self.p.name, f"bedcheck-{label or 'adhoc'}", data
        )
        sc = bedcheck.score(ref.read_bytes(), data, bc.region)
        check = Check(
            time.time(),
            label,
            sc,
            sc <= bc.threshold,
            str(snap.relative_to(self.cfg.storage_dir)),
        )
        if self.bed:
            self.bed.record(self.p.name, check)
        return check

    def bed_history(self) -> list[Check]:
        """Labelled checks taken against the current reference, newest first."""
        bc, ref = self.p.bed_check, self.reference_path
        if not (self.bed and bc and ref and ref.exists()):
            return []
        since = ref.stat().st_mtime
        return [
            c
            for c in self.bed.labelled(self.p.name, bc.min_agreements * 3)
            if c.ts >= since
        ]

    async def mark_clear(self, note: str) -> dict[str, Any]:
        """A human says the bed is empty. The first time, that frame becomes the reference; afterwards it is a
        labelled check that the vision verdict is graded against."""
        if not self.bed:
            raise ValueError("bed ledger unavailable")
        if self.status().get("state") in ("printing", "paused"):
            raise ValueError(f"{self.p.name} is printing; the bed cannot be clear")
        self.bed.set_state(self.p.name, "clear", note)
        if self.ha:
            await self.ha.clear_notification(f"print_warden_bed_{self.p.name}")
        out: dict[str, Any] = {"printer": self.p.name, "state": "clear"}
        ref = self.reference_path
        if ref and not ref.exists() and self.p.bed_check:
            try:
                await self.capture_reference()
                out["reference"] = "captured"
            except (httpx.HTTPError, OSError, ValueError) as e:
                out["reference"] = f"capture failed: {e}"
        elif check := await self.check_bed("clear"):
            out["check"] = {
                "score": round(check.score, 4),
                "clear": check.clear,
                "agrees": check.agrees,
            }
            if not check.clear:
                await self.handle(
                    Event(
                        self.p.name,
                        "bed_check_disagrees",
                        "warning",
                        f"Bed marked clear, but {check.score:.1%} of it differs from the empty-bed reference",
                        alert=True,
                        snapshot=check.snapshot,
                    )
                )
        return out

    async def _after_print(self) -> None:
        """Once the toolhead has parked: a check labelled occupied, and the phone button that clears the bed."""
        await asyncio.sleep(BED_SETTLE_S)
        try:
            await self.check_bed("occupied")
        except Exception:
            log.exception("%s post-print bed check", self.p.name)
        if self.ha:
            await self.ha.notify_actionable(
                f"Clear the bed on {self.p.name}",
                "Tap when the bed is empty. Starts stay blocked until then.",
                [(f"PRINT_WARDEN_BEDCLEAR_{self.p.name}", "Bed cleared")],
                tag=f"print_warden_bed_{self.p.name}",
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
