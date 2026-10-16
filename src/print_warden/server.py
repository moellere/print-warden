"""ASGI app: MCP (Streamable HTTP at /mcp), Obico webhook receiver, snapshot files, health.
With PRINT_WARDEN_TOKEN set, every route except /healthz, /files and the two secret-checked webhooks needs it."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import cameras, config, moonraker, policy, retention, slicer
from .bedcheck import BedStore
from .events import Event, EventStore
from .ha import HAClient, obico_event_to_alert
from .ha_events import watch_notification_actions
from .jobs import JobStore
from .spoolman import Spoolman
from .watcher import PrinterWatcher

log = logging.getLogger("print_warden")
CFG = config.load(os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml"))
STORE = EventStore(CFG.storage_dir / "events.db")
HA = HAClient(CFG.ha) if CFG.ha else None
JOBS = JobStore(CFG.storage_dir / "jobs.db")
SPOOLMAN = Spoolman(CFG.spoolman_url) if CFG.spoolman_url else None
BED = BedStore(CFG.storage_dir / "bed.db")
WATCHERS: dict[str, PrinterWatcher] = {
    n: PrinterWatcher(CFG, p, STORE, HA, JOBS, SPOOLMAN, BED)
    for n, p in CFG.printers.items()
}
UPLOADS = CFG.storage_dir / "models"
JOBDIR = CFG.storage_dir / "jobs"
PUBLIC_DIRS = ("snapshots", "timelapse", "jobs")
MODEL_SUFFIXES = (".stl", ".scad")
MAX_MODEL_BYTES = 100 * 1024 * 1024

mcp = FastMCP(
    "print-warden",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions="Print manager for the 3D printers: slice and stage jobs, start them only with the operator's "
    "permission (phone tap or quoted chat consent), and monitor via Moonraker rules, Obico verdicts and cameras. "
    "After a print the bed is occupied until the operator clears it; only the operator can say it is clear. "
    "Cancelling a running print and powering off are human actions.",
)


def _w(printer: str) -> PrinterWatcher:
    if printer not in WATCHERS:
        raise ValueError(f"unknown printer {printer!r}; known: {sorted(WATCHERS)}")
    return WATCHERS[printer]


async def _resolve_printer(
    printer_or_location: str, material: str | None = None
) -> str:
    """Accept a printer name or a location (e.g. 'wyola'). A location picks the printer there whose loaded
    material matches, preferring an idle one; ties or no match raise with the candidates listed."""
    key = printer_or_location.lower()
    if key in WATCHERS:
        return key
    cands = [n for n, w in WATCHERS.items() if (w.p.location or "").lower() == key]
    if not cands:
        raise ValueError(
            f"{printer_or_location!r} is neither a printer nor a location; printers: "
            f"{ {n: w.p.location for n, w in WATCHERS.items()} }"
        )
    ranked = []
    for n in cands:
        st = WATCHERS[n].status()
        loaded = await _loaded(n)
        ok_mat = material is None or (loaded or "").upper() == material.upper()
        idle = st.get("state") in policy.IDLE_STATES and st.get("klippy") == "ready"
        ranked.append((ok_mat, idle, n, loaded, st.get("state"), st.get("klippy")))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    best = ranked[0]
    if material is not None and not best[0]:
        raise ValueError(
            f"no printer at {key} has {material.upper()} loaded: "
            + "; ".join(f"{r[2]} has {r[3]} ({r[4]}, klippy {r[5]})" for r in ranked)
        )
    return best[2]


@mcp.tool()
async def list_printers() -> list[dict[str, Any]]:
    """Printers under watch: location, live state, loaded material, cameras, slicer qualities. Pass a location
    (e.g. "wyola") instead of a printer name to submit_job and the warden routes by where the print is needed."""
    out = []
    for n, p in CFG.printers.items():
        st = WATCHERS[n].status()
        out.append(
            {
                "name": n,
                "location": p.location,
                "state": st.get("state"),
                "klippy": st.get("klippy"),
                "moonraker_connected": st.get("moonraker_connected"),
                "loaded_material": await _loaded(n),
                "active_spool": JOBS.kv_get(f"active_spool:{n}"),
                "cameras": sorted(p.cameras),
                "qualities": sorted(p.processes),
                "materials": sorted(p.filaments),
                "light": p.light,
                "bed": BED.state(n)["state"],
                "auto_start": p.auto_start,
            }
        )
    return out


@mcp.tool()
async def print_status(printer: str = "garagex4") -> dict[str, Any]:
    """Live state of one printer: job, progress, layer, temperatures, Klipper state, connection health, mains power."""
    w = _w(printer)
    st = w.status()
    if w.p.power_device:
        try:
            st["power"] = await moonraker.power(w.p.moonraker, w.p.power_device)
        except httpx.HTTPError as e:
            st["power"] = f"unknown ({e})"
    return st


@mcp.tool()
def get_events(
    printer: str | None = None,
    since_minutes: int = 240,
    limit: int = 40,
    min_severity: str = "info",
) -> list[dict[str, Any]]:
    """Recent warden events (rule hits, Obico verdicts, state changes), newest first."""
    return STORE.query(printer, time.time() - since_minutes * 60, limit, min_severity)


@mcp.tool()
async def get_snapshot(printer: str = "garagex4", camera: str = "bed") -> Image:
    """Fresh JPEG from one of the printer's cameras ('bed' = fixed whole-bed view, 'nozzle' = C920 on the X beam)."""
    p = _w(printer).p
    if camera not in p.cameras:
        raise ValueError(f"camera must be one of {sorted(p.cameras)}")
    data = await cameras.fetch(
        p.cameras[camera], p.rotate.get(camera, 0), flip_v=p.flip_v.get(camera, False)
    )
    cameras.save(CFG.storage_dir, p.name, camera, data)
    return Image(data=data, format="jpeg")


@mcp.tool()
async def send_alert(printer: str, message: str, severity: str = "info") -> bool:
    """Push a message through the Home Assistant alert fan-out (notification, phone, Echo Pyramid for warning+)."""
    _w(printer)
    ev = Event(printer, "manual", severity, message, alert=True)
    STORE.add(ev)
    return bool(HA and await HA.alert(printer, severity, "message", message))


def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    j = dict(job)
    for k in ("preview", "gcode", "stl", "timelapse"):
        if j.get(k):
            j[k + "_url"] = (
                f"{CFG.public_url}/files/{Path(j[k]).relative_to(CFG.storage_dir)}"
            )
    j.pop("log", None)
    if j.get("est_seconds"):
        j["est_human"] = f"{j['est_seconds'] // 3600}h {j['est_seconds'] % 3600 // 60}m"
    return j


async def _spool_facts(printer: str) -> tuple[str | None, float | None]:
    """Material on the printer right now and grams left: the active spool from Spoolman, else the manual record
    (remaining weight unknown)."""
    sid = JOBS.kv_get(f"active_spool:{printer}")
    if sid and SPOOLMAN:
        try:
            spool = await SPOOLMAN.spool(int(sid))
            return spool["material"], spool.get("remaining_g")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("spoolman lookup failed: %s", e)
    return JOBS.kv_get(f"loaded_material:{printer}"), None


async def _loaded(printer: str) -> str | None:
    return (await _spool_facts(printer))[0]


async def _policy(job: dict[str, Any]) -> tuple[bool, list[str]]:
    w = _w(job["printer"])
    material, remaining = await _spool_facts(job["printer"])
    return policy.can_start(
        job,
        w.status(),
        material,
        has_power_device=bool(w.p.power_device),
        spool_remaining_g=remaining,
        bed_state=BED.state(job["printer"])["state"],
    )


async def _auto_gate(w: PrinterWatcher) -> tuple[bool, list[str], str | None]:
    """Everything a tap would otherwise vouch for: a human-cleared bed, a live check that agrees, a clean record of
    labelled checks, and the allowed hours. Returns (ok, reasons, snapshot of the live check)."""
    bc = w.p.bed_check
    if not w.p.auto_start:
        return False, [f"auto_start is off for {w.p.name}"], None
    if not bc:
        return False, [f"{w.p.name} has no bed_check configured"], None
    check = await w.check_bed()
    ok, reasons = policy.can_auto_start(
        BED.state(w.p.name)["state"],
        check,
        w.bed_history(),
        bc.min_agreements,
        w.p.auto_start_hours,
        datetime.now().astimezone(),
    )
    return ok, reasons, check.snapshot if check else None


@mcp.tool()
def bed_status(printer: str) -> dict[str, Any]:
    """Bed state (clear / occupied / unknown) and the bed check's record: labelled checks against the current
    empty-bed reference, how many agreed in a row, and whether auto_start could be considered."""
    w = _w(printer)
    hist = w.bed_history()
    streak = 0
    for c in hist:
        if not c.agrees:
            break
        streak += 1
    ref = w.reference_path
    bc = w.p.bed_check
    return {
        **BED.state(printer),
        "bed_check": bool(bc),
        "reference_captured_at": ref.stat().st_mtime if ref and ref.exists() else None,
        "labelled_checks": len(hist),
        "agreeing_in_a_row": streak,
        "min_agreements": bc.min_agreements if bc else None,
        "recent": [
            {"ts": c.ts, "label": c.label, "score": round(c.score, 4), "clear": c.clear}
            for c in hist[:5]
        ],
        "auto_start": w.p.auto_start,
        "auto_start_hours": w.p.auto_start_hours,
        "in_hours": policy.in_window(w.p.auto_start_hours, datetime.now().astimezone()),
    }


@mcp.tool()
async def check_bed(printer: str) -> dict[str, Any]:
    """Run the bed check now (unlabelled; it does not change the bed state or the record). Score is the fraction of
    the bed region that differs from the empty-bed reference."""
    w = _w(printer)
    check = await w.check_bed()
    if check is None:
        raise ValueError(
            f"{printer}: no bed check possible (bed_check config, camera or reference missing)"
        )
    return {
        "score": round(check.score, 4),
        "threshold": w.p.bed_check.threshold if w.p.bed_check else None,
        "clear": check.clear,
        "snapshot_url": f"{CFG.public_url}/files/{check.snapshot}",
    }


@mcp.tool()
async def mark_bed_clear(printer: str, authorized_by: str) -> dict[str, Any]:
    """Record that the operator has emptied the bed. `authorized_by` must quote the operator saying so. Only a human
    can know this; never call it on your own inference or from a camera image."""
    if not authorized_by or len(authorized_by.strip()) < 8:
        raise ValueError("authorized_by must quote the operator")
    return await _w(printer).mark_clear(f"chat: {authorized_by.strip()}")


@mcp.tool()
async def capture_bed_reference(printer: str, authorized_by: str) -> dict[str, Any]:
    """Take a new empty-bed reference (e.g. after swapping the build plate) and mark the bed clear. The check's record
    restarts from zero against the new reference. `authorized_by` must quote the operator confirming the bed is empty."""
    if not authorized_by or len(authorized_by.strip()) < 8:
        raise ValueError("authorized_by must quote the operator")
    w = _w(printer)
    ref = await w.capture_reference()
    BED.set_state(
        printer, "clear", f"reference captured; chat: {authorized_by.strip()}"
    )
    return {"printer": printer, "reference": str(ref.name), "state": "clear"}


@mcp.tool()
async def list_spools() -> list[dict[str, Any]]:
    """Spools known to Spoolman (id, material, name, remaining grams). Edit spools in the Spoolman UI."""
    if not SPOOLMAN:
        raise ValueError("Spoolman is not configured")
    return await SPOOLMAN.spools()


@mcp.tool()
async def set_active_spool(printer: str, spool_id: int) -> dict[str, Any]:
    """Record which Spoolman spool is physically loaded on a printer. The start policy compares its material to the job."""
    _w(printer)
    if not SPOOLMAN:
        raise ValueError("Spoolman is not configured")
    spool = await SPOOLMAN.spool(spool_id)
    JOBS.kv_set(f"active_spool:{printer}", str(spool_id))
    JOBS.kv_set(f"loaded_material:{printer}", spool["material"])
    return {"printer": printer, "active_spool": spool}


@mcp.tool()
async def loaded_material(printer: str) -> dict[str, Any]:
    """What the policy believes is loaded on a printer, and the active spool if Spoolman knows it."""
    sid = JOBS.kv_get(f"active_spool:{printer}")
    spool = await SPOOLMAN.spool(int(sid)) if (sid and SPOOLMAN) else None
    return {
        "printer": printer,
        "material": await _loaded(printer),
        "active_spool": spool,
    }


@mcp.tool()
async def submit_job(
    printer: str,
    model: str,
    material: str,
    quality: str = "standard",
    name: str | None = None,
    overrides: dict[str, Any] | None = None,
    scad_defines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Slice a model and stage the gcode on a printer (not started). `printer` is a printer name or a location
    ("covington", "wyola"): the warden routes to the printer there with the right material loaded. `model` is a file name previously
    POSTed to /upload (STL or SCAD) or an http(s) URL. `overrides` are OrcaSlicer process keys applied after the
    house rules (tree supports, no brim). Returns the job with time/filament estimates and a preview URL."""
    printer = await _resolve_printer(printer, material)
    w = _w(printer)
    p = w.p
    mat, q = material.upper(), quality.lower()
    if not p.machine or mat not in p.filaments or q not in p.processes:
        raise ValueError(
            f"{printer} slicer config: machine={p.machine}, filaments={sorted(p.filaments)}, qualities={sorted(p.processes)}"
        )
    src = await _resolve_model(model)
    stem = (name or src.stem).replace(" ", "_")
    out = JOBDIR / printer / f"{time.strftime('%Y%m%d-%H%M%S')}_{stem}"
    out.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".scad":
        stl = out / f"{stem}.stl"
        await slicer.render_scad(CFG.slicer.openscad, src, stl, scad_defines)
    else:
        stl = out / f"{stem}.stl"
        stl.write_bytes(src.read_bytes())
    res = await slicer.slice_stl(
        CFG.slicer, stl, out, p.machine, p.processes[q], p.filaments[mat], overrides
    )
    job = JOBS.create(
        printer=printer,
        name=stem,
        material=mat,
        quality=q,
        stl=str(stl),
        gcode=str(res.gcode),
        preview=str(res.preview) if res.preview else None,
        est_seconds=res.est_seconds,
        filament_g=res.filament_g,
        filament_m=res.filament_m,
        layers=res.layers,
        max_z=res.max_z,
        warnings=res.warnings,
    )
    remote = f"warden/{job['id']}_{stem}_{mat}.gcode"
    try:
        await moonraker.upload_gcode(p.moonraker, res.gcode, remote)
    except httpx.HTTPError as e:
        JOBS.transition(job["id"], "failed", f"upload failed: {e}", error=str(e))
        raise
    job = JOBS.transition(
        job["id"], "staged", f"uploaded to {printer} as {remote}", remote_name=remote
    )
    STORE.add(
        Event(
            printer,
            "job_staged",
            "info",
            f"Staged {stem} ({mat}, {q}) est {(job.get('est_seconds') or 0) // 60} min",
            {"job_id": job["id"]},
            alert=False,
        )
    )
    result = _job_public(job)
    if p.auto_start:
        try:
            started = await _start(job["id"], "auto", via_auto=True)
            result = {
                **started,
                "auto_start": {
                    "started": started["started"],
                    "blockers": started["blockers"],
                },
            }
        except httpx.HTTPError as e:
            result["auto_start"] = {
                "started": False,
                "blockers": [f"start failed: {e}"],
            }
    return result


@mcp.tool()
def list_jobs(printer: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Recent jobs, newest first, with state (sliced/staged/awaiting_approval/approved/printing/done/failed/skipped)."""
    return [_job_public(j) for j in JOBS.list(printer, limit)]


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """One job with its full transition log and the current start-policy verdict."""
    job = JOBS.get(job_id)
    ok, reasons = await _policy(job)
    return {**_job_public(job), "log": job["log"], "can_start": ok, "blockers": reasons}


@mcp.tool()
async def request_start(job_id: str) -> dict[str, Any]:
    """Ask the operator to start a staged job: sends a phone notification with Start / Skip buttons. The warden starts the
    print only when Start is tapped within the approval TTL and the deterministic policy passes at that moment."""
    job = JOBS.get(job_id)
    if job["state"] not in ("staged", "awaiting_approval"):
        raise ValueError(f"job is {job['state']}; only staged jobs can be offered")
    ok, reasons = await _policy(job)
    preview = _job_public(job).get("preview_url")
    body = (
        f"{job['name']} on {job['printer']} — {job['material']} {job['quality']}, est {_job_public(job).get('est_human')}, "
        f"{job.get('filament_g') or '?'} g. Valid {CFG.approval_ttl_minutes:.0f} min."
    )
    if not ok:
        body += " — BLOCKED: " + "; ".join(reasons)
    sent = bool(
        HA
        and await HA.notify_actionable(
            "Start print?",
            body,
            [
                (f"PRINT_WARDEN_START_{job_id}", "Start"),
                (f"PRINT_WARDEN_SKIP_{job_id}", "Skip"),
            ],
            image=preview,
            tag=f"print_warden_{job_id}",
        )
    )
    note = "asked via phone notification" if sent else "notification failed"
    asked = time.time()
    if job["state"] == "awaiting_approval":
        job = JOBS.note(job_id, note, approval_requested_at=asked)
    else:
        job = JOBS.transition(
            job_id, "awaiting_approval", note, approval_requested_at=asked
        )
    return {
        **_job_public(job),
        "notification_sent": sent,
        "policy_ok": ok,
        "blockers": reasons,
    }


@mcp.tool()
async def start_print(job_id: str, authorized_by: str) -> dict[str, Any]:
    """Start a staged job now. `authorized_by` must quote the permission the operator gave in chat (it is recorded in the
    job log); the deterministic policy still has to pass. Use request_start for the tap-to-start path instead."""
    if not authorized_by or len(authorized_by.strip()) < 8:
        raise ValueError("authorized_by must quote the operator's permission")
    return await _start(job_id, f"chat: {authorized_by.strip()}")


@mcp.tool()
async def skip_job(job_id: str, reason: str = "") -> dict[str, Any]:
    """Mark a staged or awaiting job as skipped. Never touches a running print."""
    job = JOBS.get(job_id)
    if job["state"] == "printing":
        raise ValueError(
            "job is printing; cancelling a running print is a human action"
        )
    if HA and job["state"] == "awaiting_approval":
        await HA.clear_notification(f"print_warden_{job_id}")
    return _job_public(JOBS.transition(job_id, "skipped", reason or "skipped"))


async def _start(
    job_id: str, authorized_by: str, via_tap: bool = False, via_auto: bool = False
) -> dict[str, Any]:
    """The one start path. The printer's start_lock serialises it, so a second tap or chat consent waits and then
    finds the job already printing; a tap must answer a request still inside the approval TTL; an unattended start
    must pass the auto gate as well as the policy."""
    w = _w(JOBS.get(job_id)["printer"])
    snapshot = None
    async with w.start_lock:
        job = JOBS.get(job_id)
        if via_auto:
            auto_ok, why, snapshot = await _auto_gate(w)
            if not auto_ok:
                JOBS.note(job_id, f"auto start refused: {'; '.join(why)}")
                return {**_job_public(job), "started": False, "blockers": why}
            hist = w.bed_history()
            authorized_by = f"auto: bed check clear, {len(hist)} labelled checks agree"
        if via_tap:
            refusal = None
            if job["state"] != "awaiting_approval":
                refusal = f"job is {job['state']}, not awaiting approval"
            elif policy.approval_expired(job, time.time(), CFG.approval_ttl_minutes):
                refusal = "approval request expired; request_start again"
                job = JOBS.transition(job_id, "staged", f"tap refused: {refusal}")
            if refusal:
                JOBS.note(job_id, f"start refused ({authorized_by}): {refusal}")
                return {**_job_public(job), "started": False, "blockers": [refusal]}
        ok, reasons = await _policy(job)
        if not ok:
            JOBS.note(job_id, f"start refused ({authorized_by}): {'; '.join(reasons)}")
            return {**_job_public(job), "started": False, "blockers": reasons}
        sid = JOBS.kv_get(f"active_spool:{job['printer']}")
        JOBS.transition(
            job_id,
            "approved",
            authorized_by + (f"; spool {sid}" if sid else ""),
            authorized_by=authorized_by,
            spool_id=int(sid) if sid else None,
        )
        try:
            if w.p.power_device and w.status().get("klippy") != "ready":
                up, steps = await moonraker.bring_up(w.p.moonraker, w.p.power_device)
                if not up:
                    JOBS.transition(job_id, "staged", "; ".join(steps))
                    if HA:
                        await HA.alert(
                            job["printer"],
                            "warning",
                            "printer would not come up",
                            "; ".join(steps),
                        )
                    return {
                        **_job_public(JOBS.get(job_id)),
                        "started": False,
                        "blockers": steps,
                    }
                JOBS.note(job_id, "; ".join(steps))
            await moonraker.start_print(w.p.moonraker, job["remote_name"])
        except httpx.HTTPError as e:
            JOBS.transition(job_id, "staged", f"start failed: {e}")
            raise
        job = JOBS.get(job_id)
        if job["state"] != "printing":
            job = JOBS.transition(
                job_id, "printing", "print/start sent", started_at=time.time()
            )
    STORE.add(
        Event(
            job["printer"],
            "job_started",
            "info",
            f"Started {job['name']} ({authorized_by})",
            {"job_id": job_id},
            alert=True,
        )
    )
    if HA:
        await HA.clear_notification(f"print_warden_{job_id}")
        await HA.alert(
            job["printer"],
            "warning" if via_auto else "info",
            "print auto-started" if via_auto else "print started",
            f"{job['name']} started ({authorized_by})",
            f"{CFG.public_url}/files/{snapshot}" if snapshot else None,
        )
    return {**_job_public(job), "started": True, "blockers": []}


async def _on_ha_action(action: str, data: dict[str, Any]) -> None:
    verb, _, job_id = action.removeprefix("PRINT_WARDEN_").partition("_")
    try:
        if verb == "START":
            res = await _start(job_id, "phone tap", via_tap=True)
            if not res["started"] and HA:
                await HA.alert(
                    JOBS.get(job_id)["printer"],
                    "warning",
                    "start refused",
                    "; ".join(res["blockers"]),
                )
        elif verb == "SKIP":
            JOBS.transition(job_id, "skipped", "phone tap")
        elif verb == "BEDCLEAR":
            await _w(job_id).mark_clear("phone tap")
    except (KeyError, ValueError, httpx.HTTPError) as e:
        log.warning("HA action %s: %s", action, e)


def _model_name(name: str) -> str:
    base = Path(name.split("?")[0]).name
    if Path(base).suffix.lower() not in MODEL_SUFFIXES:
        raise ValueError(f"{name!r}: models must be {' or '.join(MODEL_SUFFIXES)}")
    return base


async def _resolve_model(model: str) -> Path:
    if model.startswith(("http://", "https://")):
        name = _model_name(httpx.URL(model).path)
        buf = bytearray()
        async with (
            httpx.AsyncClient(timeout=60, follow_redirects=True) as c,
            c.stream("GET", model) as r,
        ):
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_MODEL_BYTES:
                    raise ValueError(f"{model} exceeds {MAX_MODEL_BYTES >> 20} MB")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dst = UPLOADS / name
        dst.write_bytes(bytes(buf))
        return dst
    p = UPLOADS / _model_name(model)
    if not p.exists():
        raise FileNotFoundError(
            f"{model} not found; POST it to {CFG.public_url}/upload first"
        )
    return p


async def upload(request: Request) -> JSONResponse:
    length = request.headers.get("content-length") or "0"
    if not length.isdigit() or int(length) > MAX_MODEL_BYTES + 65536:
        return JSONResponse({"error": "file too large"}, status_code=413)
    form = await request.form()
    f = form.get("file")
    if not isinstance(f, UploadFile) or not f.filename:
        return JSONResponse(
            {"error": "multipart field 'file' required"}, status_code=400
        )
    try:
        name = _model_name(f.filename)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    data = await f.read()
    if len(data) > MAX_MODEL_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / name).write_bytes(data)
    return JSONResponse({"ok": True, "model": name})


def _secret_ok(request: Request, secret: str | None) -> bool:
    """Fail closed: an unset secret disables the route."""
    given = request.query_params.get("secret") or ""
    return bool(secret) and hmac.compare_digest(given.encode(), (secret or "").encode())


async def ha_action(request: Request) -> JSONResponse:
    if not _secret_ok(request, CFG.ha.action_secret if CFG.ha else None):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    payload = await request.json()
    await _on_ha_action(str(payload.get("action", "")), payload)
    return JSONResponse({"ok": True})


async def obico_webhook(request: Request) -> JSONResponse:
    if not _secret_ok(request, CFG.obico_webhook_secret):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    payload = await request.json()
    name = (payload.get("printer") or {}).get("name") or ""
    w = next((w for n, w in WATCHERS.items() if n.lower() == name.lower()), None)
    kind, sev, msg, img = obico_event_to_alert(payload)
    ev = Event(
        w.p.name if w else name or "unknown",
        kind,
        sev,
        msg,
        {"obico": payload, "img_url": img},
        alert=sev != "info",
    )
    if w:
        await w.handle(ev)
    else:
        STORE.add(ev)
    return JSONResponse({"ok": True})


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "printers": {n: w.mr.connected for n, w in WATCHERS.items()}}
    )


async def expire_approvals(now: float | None = None) -> list[str]:
    """Return unanswered start requests older than the TTL to staged and withdraw their phone notifications."""
    now = now or time.time()
    expired = []
    for job in JOBS.list(None, 200, ("awaiting_approval",)):
        w = WATCHERS.get(job["printer"])
        if (w and w.start_lock.locked()) or not policy.approval_expired(
            job, now, CFG.approval_ttl_minutes
        ):
            continue
        JOBS.transition(job["id"], "staged", "approval request expired")
        expired.append(job["id"])
        if HA:
            await HA.clear_notification(f"print_warden_{job['id']}")
    return expired


async def housekeeping(interval_s: float = 60) -> None:
    last_prune = 0.0
    while True:
        try:
            await expire_approvals()
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                rows = STORE.prune(CFG.keep_days)
                files = await asyncio.to_thread(
                    retention.prune_files, CFG.storage_dir, CFG.keep_days
                )
                log.info("retention: %d events, %d files pruned", rows, files)
        except Exception:
            log.exception("housekeeping")
        await asyncio.sleep(interval_s)


class TokenAuth:
    """Bearer token (or ?token=, for clients that can only set a URL) on everything but the open paths."""

    OPEN = ("/healthz", "/obico/webhook", "/ha/action")

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = (token or "").encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token or self._open(scope["path"]):
            await self.app(scope, receive, send)
            return
        req = Request(scope)
        auth = req.headers.get("authorization", "")
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
        given = given or req.query_params.get("token", "")
        if hmac.compare_digest(given.encode(), self.token):
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": "unauthorized"}, status_code=401)(
            scope, receive, send
        )

    def _open(self, path: str) -> bool:
        return path in self.OPEN or path.startswith("/files/")


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    for what, ok in (
        (
            "PRINT_WARDEN_TOKEN unset: /mcp and /upload are open to the network",
            CFG.api_token,
        ),
        (
            "OBICO_WEBHOOK_SECRET unset: /obico/webhook is disabled",
            CFG.obico_webhook_secret,
        ),
        (
            "HA_ACTION_SECRET unset: /ha/action is disabled",
            CFG.ha and CFG.ha.action_secret,
        ),
    ):
        if not ok:
            log.warning(what)
    tasks = [
        asyncio.create_task(w.run(), name=f"watch:{n}") for n, w in WATCHERS.items()
    ]
    tasks.append(asyncio.create_task(housekeeping(), name="housekeeping"))
    if CFG.ha and CFG.ha.token:
        tasks.append(
            asyncio.create_task(
                watch_notification_actions(CFG.ha, _on_ha_action), name="ha-events"
            )
        )
    async with mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()
    if HA:
        await HA.aclose()


for _d in PUBLIC_DIRS:
    (CFG.storage_dir / _d).mkdir(parents=True, exist_ok=True)
# Only these subtrees are served: the databases and uploaded models never are. /files stays tokenless because the
# phone fetches notification images by URL.
app = Starlette(
    routes=[
        Route("/healthz", health),
        Route("/obico/webhook", obico_webhook, methods=["POST"]),
        Route("/upload", upload, methods=["POST"]),
        Route("/ha/action", ha_action, methods=["POST"]),
        *(
            Mount(f"/files/{d}", app=StaticFiles(directory=str(CFG.storage_dir / d)))
            for d in PUBLIC_DIRS
        ),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)
asgi = TokenAuth(app, CFG.api_token)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(asgi, host=CFG.host, port=CFG.port, log_level="warning")
