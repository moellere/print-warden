"""Per-printer orchestration: Moonraker updates -> rules -> events, snapshots, alerts, light, timelapse, job
tracking, spool usage, and job reconciliation whenever the Moonraker connection (re)starts."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import (
    bedcheck,
    cameras,
    footprint,
    moonraker,
    outcome,
    park,
    policy,
    reconcile,
    timelapse,
)
from .bedcheck import BedStore, Check
from .config import Config, Printer
from .events import Event, EventStore
from .ha import HAClient
from .jobs import JobStore
from .moonraker import MoonrakerClient
from .rules import RuleEngine
from .spoolman import Spoolman

log = logging.getLogger(__name__)
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
        self.check_skipped: str | None = None
        self.rehomed_z: float | None = None
        self.start_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.cfg = cfg
        self.p = printer
        self.store = store
        self.ha = ha
        self.rules = RuleEngine(printer.name, printer.rules)
        self.mr = MoonrakerClient(printer.ws_url, self._on_update, printer.moonraker)
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
                self._spawn(self._after_print(job, ev.kind))
            used = ev.data.get("filament_used_mm")
            if job and self.ha:
                self._spawn(self._outcome_prompt(job, ev, cam))
            if job:
                await self.settle_spool(job, used, ev.data.get("print_duration"))
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
        if ev.kind == "first_layer_check":
            self._spawn(self._first_layer_prompt(ev))
        elif ev.kind == "obico_failure" and (
            (ev.data.get("obico") or {}).get("event") or {}
        ).get("print_paused"):
            self._spawn(self._pause_prompt(ev))

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

    async def settle_spool(
        self,
        job: dict[str, Any],
        used_mm: float | None,
        duration_s: float | None = None,
    ) -> None:
        """Record what the print took (time, filament, grams, cost) and deduct it from the job's spool. Never raises."""
        if not self.jobs:
            return
        fields: dict[str, Any] = {}
        if duration_s:
            fields["print_duration_s"] = float(duration_s)
        if used_mm is None:
            if fields:
                self.jobs.note(job["id"], f"printed {duration_s:.0f} s", **fields)
            return
        grams = used_grams(job, used_mm)
        fields.update(filament_used_mm=used_mm, grams_used=grams)
        sid = job.get("spool_id")
        spool: dict[str, Any] | None = None
        note = f"used {used_mm:.0f} mm"
        if self.spoolman and sid:
            try:
                if self.p.track_spool_usage:
                    spool = await self.spoolman.use_length(int(sid), used_mm)
                    note = f"spool {sid}: -{used_mm:.0f} mm, {spool.get('remaining_g') or 0:.0f} g left"
                else:
                    spool = await self.spoolman.spool(int(sid))
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("%s spool %s failed: %s", self.p.name, sid, e)
                note = f"spool {sid} update for {used_mm:.0f} mm failed: {e}"
        if spool and grams is not None and spool.get("price_per_g"):
            fields["cost"] = round(grams * spool["price_per_g"], 2)
        self.jobs.note(job["id"], note, **fields)

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
            state, note, used, duration = verdict
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
                await self.settle_spool(job, used, duration)
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

    async def ensure_pose(self) -> tuple[bool, str]:
        """Is the bed where the reference was taken? With rehome_y, an idle printer whose steppers were released is
        brought back by homing Y (and X if the pose sets it) and moving there; Z is never moved."""
        bc = self.p.bed_check
        if not bc or not bc.pose:
            return True, "no pose configured"
        th = self.mr.state.get("toolhead", {})
        if "z" in (th.get("homed_axes") or ""):
            self.rehomed_z = None
        ok, why = park.status(th, bc.pose, self.rehomed_z)
        if ok or not bc.rehome_y:
            return ok, why
        st = self.status()
        pos = th.get("position") or []
        if st.get("klippy") != "ready" or st.get("state") in ("printing", "paused"):
            return (
                False,
                f"{why}; not rehoming: printer is {st.get('state') or st.get('klippy')}",
            )
        if len(pos) < 3 or pos[2] < bc.pose.z - park.TOLERANCE_MM:
            return False, f"{why}; not rehoming: last Z is below the pose"
        try:
            await moonraker.gcode_script(self.p.moonraker, park.rehome_script(bc.pose))
        except httpx.HTTPError as e:
            return False, f"{why}; rehome failed: {e}"
        self.rehomed_z = float(pos[2])
        await asyncio.sleep(1)
        ok, why = park.status(
            self.mr.state.get("toolhead", {}), bc.pose, self.rehomed_z
        )
        await self.handle(
            Event(
                self.p.name,
                "bed_rehomed",
                "warning",
                f"Homed Y and moved the bed to the check pose (Z untouched at {pos[2]:.0f}): {why}",
                alert=True,
            )
        )
        return ok, why

    async def capture_reference(self) -> Path:
        bc, ref = self.p.bed_check, self.reference_path
        if not bc or not ref:
            raise ValueError(f"{self.p.name} has no bed_check configured")
        if bc.camera not in self.p.cameras:
            raise ValueError(
                f"bed_check camera {bc.camera!r} is not one of {sorted(self.p.cameras)}"
            )
        at_pose, why = await self.ensure_pose()
        if not at_pose:
            raise ValueError(f"not at the bed-check pose: {why}")
        data = await self._lit_frame(bc.camera)
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(data)
        return ref

    async def check_bed(self, label: str | None = None) -> Check | None:
        """Compare the bed camera to the empty-bed reference. `label` is what a human knows the bed to be right
        now; labelled checks are the record auto_start is judged on. None when there is nothing to compare."""
        bc, ref = self.p.bed_check, self.reference_path
        self.check_skipped = None
        if not (bc and ref and ref.exists() and bc.camera in self.p.cameras):
            self.check_skipped = (
                "bed_check config, camera or empty-bed reference missing"
            )
            return None
        at_pose, why = await self.ensure_pose()
        if not at_pose:
            self.check_skipped = f"not at the bed-check pose: {why}"
            return None
        try:
            data = await self._lit_frame(bc.camera)
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.warning("%s bed check: camera failed: %s", self.p.name, e)
            self.check_skipped = f"camera failed: {e}"
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
        elif (check := await self.check_bed("clear")) is None:
            out["check"] = {"skipped": self.check_skipped}
        else:
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

    async def _after_print(
        self, job: dict[str, Any] | None = None, kind: str | None = None
    ) -> None:
        """Once the toolhead has parked: a check labelled occupied, the footprint check of a completed print (its
        hint re-sends the outcome prompt), and the phone button that clears the bed."""
        await asyncio.sleep(BED_SETTLE_S)
        check = None
        try:
            check = await self.check_bed("occupied")
        except Exception:
            log.exception("%s post-print bed check", self.p.name)
        if check and job and kind == "print_complete":
            try:
                verdict = self.footprint_verdict(job, check)
            except (OSError, ValueError) as e:
                log.warning("%s footprint check: %s", self.p.name, e)
                verdict = None
            if verdict and verdict.hint and self.ha:
                await self.ha.notify_actionable(
                    f"How did {job['name']} turn out?",
                    f"{self.p.name}: {verdict.hint}. Confirm below.",
                    outcome.first(job["id"], True),
                    image=f"{self.cfg.public_url}/files/{check.snapshot}"
                    if check.snapshot
                    else None,
                    tag=outcome.tag(job["id"]),
                )
        if self.ha:
            await self.ha.notify_actionable(
                f"Clear the bed on {self.p.name}",
                "Tap when the bed is empty. Starts stay blocked until then.",
                [(f"PRINT_WARDEN_BEDCLEAR_{self.p.name}", "Bed cleared")],
                tag=f"print_warden_bed_{self.p.name}",
            )

    def footprint_verdict(
        self, job: dict[str, Any], check: Check
    ) -> footprint.Verdict | None:
        """The part-on-bed verdict for a finished job against a bed-check frame, recorded on the job; None without a
        camera calibration, reference or gcode."""
        raw = self.jobs.kv_get(f"bed_calibration:{self.p.name}") if self.jobs else None
        ref = self.reference_path
        if not (raw and ref and ref.exists() and check.snapshot and job.get("gcode")):
            return None
        cal = json.loads(raw)
        bed = tuple(cal["bed"])
        h = footprint.calibration([tuple(c) for c in cal["corners"]], bed)
        verdict = footprint.analyse(
            ref.read_bytes(),
            (self.cfg.storage_dir / check.snapshot).read_bytes(),
            h,
            footprint.first_layer(Path(job["gcode"]).read_text(errors="ignore")),
            bed,
        )
        if self.jobs:
            self.jobs.note(
                job["id"],
                f"footprint check: {verdict.inside:.0%} of the footprint and {verdict.outside:.0%} of the rest "
                "of the bed changed",
                outcome_hint=verdict.hint,
            )
        return verdict

    async def _outcome_prompt(
        self, job: dict[str, Any], ev: Event, cam: str | None
    ) -> None:
        """The end photo with Good / Failed (or the failure reasons, for a cancelled or errored print). Sent whatever
        the quiet hours: it waits on the phone until someone looks, like the bed-clear prompt."""
        if not self.ha:
            return
        snap = (
            self.cfg.storage_dir / ev.snapshot
            if ev.snapshot
            else (await self._snap(cam, None, None) if cam else None)
        )
        image = (
            f"{self.cfg.public_url}/files/{snap.relative_to(self.cfg.storage_dir)}"
            if snap
            else None
        )
        completed = ev.kind == "print_complete"
        await self.ha.notify_actionable(
            f"How did {job['name']} turn out?"
            if completed
            else f"Why did {job['name']} fail?",
            f"{self.p.name}: {ev.kind.removeprefix('print_')}. The label feeds the failure stats.",
            outcome.first(job["id"], completed),
            image=image,
            tag=outcome.tag(job["id"]),
        )

    async def _first_layer_prompt(self, ev: Event) -> None:
        """A look at the first layers with Looks good / Cancel buttons bound to the printing job. Skipped in quiet
        hours and for prints the warden did not start a job for."""
        job = self.jobs.find_active(self.p.name) if self.jobs else None
        if not (self.ha and job) or await self.quiet():
            return
        cam = (
            "nozzle" if "nozzle" in self.p.cameras else next(iter(self.p.cameras), None)
        )
        snap = await self._snap(cam, None, None) if cam else None
        image = (
            f"{self.cfg.public_url}/files/{snap.relative_to(self.cfg.storage_dir)}"
            if snap
            else None
        )
        await self.ha.notify_actionable(
            f"First layers on {self.p.name}",
            f"{job['name']} is at layer {ev.data.get('layer')}. Does it look right?",
            [
                (f"PRINT_WARDEN_FIRSTLAYER_{job['id']}", "Looks good"),
                (f"PRINT_WARDEN_CANCEL_{job['id']}", "Cancel print"),
            ],
            image=image,
            tag=f"print_warden_fl_{job['id']}",
        )

    async def _pause_prompt(self, ev: Event) -> None:
        """Obico paused the print: offer Resume / Cancel, bound to the job, regardless of quiet hours."""
        job = self.jobs.find_active(self.p.name) if self.jobs else None
        if not (self.ha and job):
            return
        image = ev.data.get("img_url") or (
            f"{self.cfg.public_url}/files/{ev.snapshot}" if ev.snapshot else None
        )
        await self.ha.notify_actionable(
            f"Obico paused {self.p.name}",
            f"{job['name']}: {ev.message}. Resume only if the print is fine.",
            [
                (f"PRINT_WARDEN_RESUME_{job['id']}", "Resume"),
                (f"PRINT_WARDEN_CANCEL_{job['id']}", "Cancel print"),
            ],
            image=image,
            tag=f"print_warden_pause_{job['id']}",
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

    async def quiet(self) -> bool:
        """Inside the quiet_hours window, or the quiet_entity in Home Assistant is in one of quiet_states. An
        unreadable entity is not quiet: alerts fail loud."""
        if policy.in_window(self.cfg.quiet_hours, datetime.now().astimezone()):
            return True
        if not (self.ha and self.cfg.quiet_entity):
            return False
        return await self.ha.state(self.cfg.quiet_entity) in self.cfg.quiet_states

    async def _alert(self, ev: Event) -> None:
        """Critical always goes out; info and warning respect the per-kind cooldown and quiet hours."""
        if not self.ha:
            return
        now = time.time()
        if ev.severity != "critical" and (
            now - self.last_alert.get(ev.kind, 0) < self.cfg.alert_cooldown_s
            or await self.quiet()
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
