"""ASGI app: MCP (Streamable HTTP at /mcp), Obico webhook receiver, snapshot files, health."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import cameras, config, moonraker, policy, slicer
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
WATCHERS: dict[str, PrinterWatcher] = {
    n: PrinterWatcher(CFG, p, STORE, HA, JOBS) for n, p in CFG.printers.items()
}
UPLOADS = CFG.storage_dir / "models"
JOBDIR = CFG.storage_dir / "jobs"

mcp = FastMCP(
    "print-warden",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions="Print manager for the 3D printers: slice and stage jobs, start them only with the operator's "
    "permission (phone tap or quoted chat consent), and monitor via Moonraker rules, Obico verdicts and cameras. "
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
    for k in ("preview", "gcode", "stl"):
        if j.get(k):
            j[k + "_url"] = (
                f"{CFG.public_url}/files/{Path(j[k]).relative_to(CFG.storage_dir)}"
            )
    j.pop("log", None)
    if j.get("est_seconds"):
        j["est_human"] = f"{j['est_seconds'] // 3600}h {j['est_seconds'] % 3600 // 60}m"
    return j


async def _loaded(printer: str) -> str | None:
    """Material on the printer right now: the active spool's material from Spoolman, else the manual record."""
    sid = JOBS.kv_get(f"active_spool:{printer}")
    if sid and SPOOLMAN:
        try:
            return (await SPOOLMAN.spool(int(sid)))["material"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("spoolman lookup failed: %s", e)
    return JOBS.kv_get(f"loaded_material:{printer}")


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
        layers=res.layers,
        max_z=res.max_z,
        warnings=res.warnings,
    )
    remote = f"warden/{job['id']}_{stem}_{mat}.gcode"
    await moonraker.upload_gcode(p.moonraker, res.gcode, remote)
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
    return _job_public(job)


@mcp.tool()
def list_jobs(printer: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Recent jobs, newest first, with state (sliced/staged/awaiting_approval/approved/printing/done/failed/skipped)."""
    return [_job_public(j) for j in JOBS.list(printer, limit)]


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """One job with its full transition log and the current start-policy verdict."""
    job = JOBS.get(job_id)
    st = _w(job["printer"]).status()
    ok, reasons = policy.can_start(
        job,
        st,
        await _loaded(job["printer"]),
        has_power_device=bool(_w(job["printer"]).p.power_device),
    )
    return {**_job_public(job), "log": job["log"], "can_start": ok, "blockers": reasons}


@mcp.tool()
async def request_start(job_id: str) -> dict[str, Any]:
    """Ask the operator to start a staged job: sends a phone notification with Start / Skip buttons. The warden starts the
    print only when Start is tapped and the deterministic policy passes at that moment."""
    job = JOBS.get(job_id)
    st = _w(job["printer"]).status()
    ok, reasons = policy.can_start(
        job,
        st,
        await _loaded(job["printer"]),
        has_power_device=bool(_w(job["printer"]).p.power_device),
    )
    preview = _job_public(job).get("preview_url")
    body = f"{job['name']} on {job['printer']} — {job['material']} {job['quality']}, est {_job_public(job).get('est_human')}, {job.get('filament_g') or '?'} g"
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
    job = JOBS.transition(
        job_id,
        "awaiting_approval",
        "asked via phone notification" if sent else "notification failed",
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
def skip_job(job_id: str, reason: str = "") -> dict[str, Any]:
    """Mark a staged or awaiting job as skipped. Never touches a running print."""
    job = JOBS.get(job_id)
    if job["state"] == "printing":
        raise ValueError(
            "job is printing; cancelling a running print is a human action"
        )
    return _job_public(JOBS.transition(job_id, "skipped", reason or "skipped"))


async def _start(job_id: str, authorized_by: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    w = _w(job["printer"])
    ok, reasons = policy.can_start(
        job,
        w.status(),
        await _loaded(job["printer"]),
        has_power_device=bool(w.p.power_device),
    )
    if not ok:
        JOBS.transition(
            job_id,
            job["state"],
            f"start refused ({authorized_by}): {'; '.join(reasons)}",
        )
        return {**_job_public(job), "started": False, "blockers": reasons}
    JOBS.transition(job_id, "approved", authorized_by, authorized_by=authorized_by)
    if sid := JOBS.kv_get(f"active_spool:{job['printer']}"):
        JOBS.transition(job_id, "approved", f"spool {sid}")
    if w.p.power_device and w.status().get("klippy") != "ready":
        up, steps = await moonraker.bring_up(w.p.moonraker, w.p.power_device)
        JOBS.transition(job_id, "approved" if up else "staged", "; ".join(steps))
        if not up:
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
    await moonraker.start_print(w.p.moonraker, job["remote_name"])
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
        await HA.alert(
            job["printer"],
            "info",
            "print started",
            f"{job['name']} started ({authorized_by})",
        )
    return {**_job_public(job), "started": True, "blockers": []}


async def _on_ha_action(action: str, data: dict[str, Any]) -> None:
    verb, _, job_id = action.removeprefix("PRINT_WARDEN_").partition("_")
    try:
        if verb == "START":
            res = await _start(job_id, "phone tap")
            if not res["started"] and HA:
                await HA.alert(
                    JOBS.get(job_id)["printer"],
                    "warning",
                    "start refused",
                    "; ".join(res["blockers"]),
                )
        elif verb == "SKIP":
            JOBS.transition(job_id, "skipped", "phone tap")
    except (KeyError, ValueError, httpx.HTTPError) as e:
        log.warning("HA action %s: %s", action, e)


async def _resolve_model(model: str) -> Path:
    if model.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(model)
            r.raise_for_status()
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dst = UPLOADS / Path(model).name.split("?")[0]
        dst.write_bytes(r.content)
        return dst
    p = UPLOADS / Path(model).name
    if not p.exists():
        raise FileNotFoundError(
            f"{model} not found; POST it to {CFG.public_url}/upload first"
        )
    return p


async def upload(request: Request) -> JSONResponse:
    form = await request.form()
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        return JSONResponse(
            {"error": "multipart field 'file' required"}, status_code=400
        )
    name = Path(f.filename).name
    if Path(name).suffix.lower() not in (".stl", ".scad", ".3mf"):
        return JSONResponse({"error": "stl, scad or 3mf only"}, status_code=400)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / name).write_bytes(await f.read())
    return JSONResponse({"ok": True, "model": name})


async def ha_action(request: Request) -> JSONResponse:
    if (
        CFG.obico_webhook_secret
        and request.query_params.get("secret") != CFG.obico_webhook_secret
    ):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    payload = await request.json()
    await _on_ha_action(str(payload.get("action", "")), payload)
    return JSONResponse({"ok": True})


async def obico_webhook(request: Request) -> JSONResponse:
    if (
        CFG.obico_webhook_secret
        and request.query_params.get("secret") != CFG.obico_webhook_secret
    ):
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


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    tasks = [
        asyncio.create_task(w.run(), name=f"watch:{n}") for n, w in WATCHERS.items()
    ]
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


CFG.storage_dir.mkdir(parents=True, exist_ok=True)
app = Starlette(
    routes=[
        Route("/healthz", health),
        Route("/obico/webhook", obico_webhook, methods=["POST"]),
        Route("/upload", upload, methods=["POST"]),
        Route("/ha/action", ha_action, methods=["POST"]),
        Mount("/files", app=StaticFiles(directory=str(CFG.storage_dir)), name="files"),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning")
