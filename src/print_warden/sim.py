"""A simulated printer farm for `print-warden demo`: Moonraker, Home Assistant and a slicer, in one process.

- **Moonraker** (`/moonraker/<printer>/…`): the HTTP routes and websocket the warden uses. A started file is played
  layer by layer: `M104`/`M140` set heater targets (the heaters reach them at once), `;Z:` marks are layers, `E` adds
  filament. A move past the bed in X or Y stops the print with Klipper's "Move out of range" in the gcode store.
  `PAUSE`, `RESUME`, `CANCEL_PRINT`, `TURN_OFF_HEATERS`, `M112` (a shutdown) and `FIRMWARE_RESTART` behave as on
  Klipper; a cancel or an error leaves the heaters on, as Klipper's own `CANCEL_PRINT` does. Finished prints go to
  the history.
- **Cameras** (`/sim/camera/<printer>/<camera>.jpg`): the bed plate, with the part on it from the first layer until
  it is taken off (`/sim/bed`, or the phone's button), so bed checks and snapshots have something to look at.
- **Home Assistant** (`/api/…`): alerts, notifications (with their buttons), entity states and the websocket that
  carries notification taps. `/sim/phone` shows the notifications with their buttons, the alerts and each printer,
  with a console; `/sim/tap`, `/sim/gcode` and `/sim/notifications` do the same for scripts.
- **Slicer** (`orca: sim`): writes a gcode for the model's bounding box, centred on the bed: a perimeter per layer
  at the process's layer height, the filament's temperatures, the machine's end gcode. No OrcaSlicer needed.

Time runs `speed` times faster than a print would: a layer takes `LAYER_S / speed` seconds.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

ORCA = "sim"
LAYER_S = 6.0
KLIPPER = "v0.12.0-sim"
_MOVE = re.compile(r"^G[01]\b", re.IGNORECASE)
_AXIS = re.compile(r"([XYZE])(-?\d*\.?\d+)", re.IGNORECASE)
_TEMP = re.compile(r"^(M104|M109|M140|M190)\b.*?\bS(\d+\.?\d*)", re.IGNORECASE)


def _now() -> float:
    return time.time()


@dataclass
class SimPrinter:
    name: str
    bed: tuple[float, float] = (220.0, 220.0)
    speed: float = 12.0
    settings: dict[str, Any] = field(
        default_factory=lambda: {
            "printer": {
                "kinematics": "cartesian",
                "max_velocity": 300,
                "max_accel": 3000,
            },
            "extruder": {"rotation_distance": 7.71, "nozzle_diameter": 0.4},
        }
    )
    files: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    store: list[dict[str, Any]] = field(default_factory=list)
    clients: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    state: dict[str, Any] = field(default_factory=dict)
    part_on_bed: bool = False
    _task: asyncio.Task[None] | None = None
    _resume: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: str | None = None

    def __post_init__(self) -> None:
        self.state = {
            "print_stats": {
                "state": "standby", "filename": "", "print_duration": 0.0, "total_duration": 0.0,
                "filament_used": 0.0, "message": "", "info": {"current_layer": None, "total_layer": None},
            },
            "display_status": {"progress": 0.0, "message": None},
            "virtual_sdcard": {"progress": 0.0, "is_active": False},
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "extruder": {"temperature": 22.0, "target": 0.0, "pressure_advance": 0.04},
            "heater_bed": {"temperature": 22.0, "target": 0.0},
            "toolhead": {"homed_axes": "", "position": [0.0, 0.0, 0.0, 0.0]},
            "idle_timeout": {"state": "Idle"},
            "gcode_move": {"gcode_position": [0.0, 0.0, 0.0, 0.0]},
            "configfile": {"settings": self.settings, "save_config_pending": False},
            "bed_mesh": {"profiles": {"default": {"points": [[0.0, 0.02], [0.01, -0.01]]}}},
            "mcu": {"mcu_version": KLIPPER},
        }  # fmt: skip
        self._resume.set()

    # what the warden sees

    def _set(self, obj: str, **fields: Any) -> None:
        self.state[obj].update(fields)
        self._push({obj: dict(fields)})

    def _push(
        self, changed: dict[str, Any], method: str = "notify_status_update"
    ) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": [changed, _now()]}
        for q in list(self.clients):
            q.put_nowait(msg)

    def _signal(self, method: str) -> None:
        for q in list(self.clients):
            q.put_nowait({"jsonrpc": "2.0", "method": method})

    def _log(self, message: str, kind: str = "command") -> None:
        self.store.append({"message": message, "time": _now(), "type": kind})
        del self.store[:-200]

    def query(self, objects: Iterable[str]) -> dict[str, Any]:
        return {
            o: json.loads(json.dumps(self.state[o])) for o in objects if o in self.state
        }

    @property
    def printing(self) -> bool:
        return self.state["print_stats"]["state"] in ("printing", "paused")

    # gcode

    def _heat(self, line: str) -> None:
        if m := _TEMP.match(line):
            t = float(m[2])
            heater = "extruder" if m[1].upper() in ("M104", "M109") else "heater_bed"
            self._set(heater, target=t, temperature=t if t else 22.0)

    def script(self, text: str) -> None:
        """The console: Klipper's commands as one gcode script, line by line."""
        for raw in text.splitlines():
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            self._log(line)
            cmd = line.split()[0].upper()
            if self.state["webhooks"]["state"] != "ready" and cmd != "FIRMWARE_RESTART":
                self._log("!! Printer is not ready", "response")
                continue
            if cmd == "M112":
                self.shutdown("Shutdown due to M112 command")
            elif cmd == "FIRMWARE_RESTART":
                self.restart()
            elif cmd == "PAUSE":
                self.pause()
            elif cmd == "RESUME":
                self.resume()
            elif cmd == "CANCEL_PRINT":
                self.cancel()
            elif cmd == "TURN_OFF_HEATERS":
                self._set("extruder", target=0.0, temperature=22.0)
                self._set("heater_bed", target=0.0, temperature=22.0)
            elif cmd == "G28":
                self._set("toolhead", homed_axes="xyz")
            elif cmd.startswith("SET_SIM_"):
                # SET_SIM_CONFIG SECTION=printer KEY=max_accel VALUE=5000: what a printer.cfg edit and restart does
                args = dict(a.split("=", 1) for a in line.split()[1:] if "=" in a)
                section = self.settings.setdefault(args.get("SECTION", "printer"), {})
                section[args.get("KEY", "max_accel")] = args.get("VALUE", "")
                self._set("configfile", settings=self.settings)
            else:
                self._heat(line)

    def shutdown(self, message: str) -> None:
        if self._task:
            self._stop = "error"
            self._resume.set()
        self._set("webhooks", state="shutdown", state_message=message)
        self._log(f"!! {message}", "response")
        self._signal("notify_klippy_shutdown")

    def restart(self) -> None:
        self._set("webhooks", state="ready", state_message="Printer is ready")
        self._set("print_stats", state="standby", message="")
        self._signal("notify_klippy_ready")

    # printing

    def start(self, filename: str) -> None:
        if filename not in self.files:
            raise KeyError(f"file {filename} not found")
        if self.printing:
            raise ValueError("Printer is busy")
        if self.state["webhooks"]["state"] != "ready":
            raise ValueError("Klipper is not ready")
        self._stop = None
        self._resume.set()
        self._task = asyncio.get_running_loop().create_task(self._play(filename))

    def pause(self) -> None:
        if self.state["print_stats"]["state"] == "printing":
            self._resume.clear()
            self._set("print_stats", state="paused")
            self._set("idle_timeout", state="Ready")

    def resume(self) -> None:
        if self.state["print_stats"]["state"] == "paused":
            self._set("print_stats", state="printing")
            self._set("idle_timeout", state="Printing")
            self._resume.set()

    def cancel(self) -> None:
        if self.printing:
            self._stop = "cancelled"
            self._resume.set()

    async def _play(self, filename: str) -> None:
        lines = self.files[filename].splitlines()
        total = sum(1 for ln in lines if ln.startswith(";Z:")) or None
        started = _now()
        self._set(
            "print_stats", state="printing", filename=filename, print_duration=0.0, filament_used=0.0,
            message="", info={"current_layer": 0, "total_layer": total},
        )  # fmt: skip
        self._set("virtual_sdcard", progress=0.0, is_active=True)
        self._set("idle_timeout", state="Printing")
        layer, used, duration = 0, 0.0, 0.0
        status = "completed"
        x = y = 0.0
        relative = False
        for i, raw in enumerate(lines):
            await self._resume.wait()
            if self._stop:
                status = self._stop
                break
            line = raw.split(";", 1)[0].strip()
            if raw.startswith(";Z:"):
                layer += 1
                self.part_on_bed = True
                duration += LAYER_S
                await asyncio.sleep(LAYER_S / self.speed)
                self._set("print_stats", print_duration=duration, total_duration=_now() - started,
                          filament_used=round(used, 2), info={"current_layer": layer, "total_layer": total})  # fmt: skip
                self._set("virtual_sdcard", progress=round(i / len(lines), 4))
                self._set("display_status", progress=round(i / len(lines), 4))
                continue
            if not line:
                continue
            up = line.upper()
            if up.startswith("M83"):
                relative = True
            elif up.startswith("M82"):
                relative = False
            elif _TEMP.match(line):
                self._heat(line)
            elif up.startswith("G28"):
                self._set("toolhead", homed_axes="xyz")
            elif _MOVE.match(line):
                axes = {k.upper(): float(v) for k, v in _AXIS.findall(line)}
                nx, ny = axes.get("X", x), axes.get("Y", y)
                if not (-5 <= nx <= self.bed[0] + 5 and -5 <= ny <= self.bed[1] + 5):
                    msg = f"Move out of range: {nx:.3f} {ny:.3f} {axes.get('Z', 0.0):.3f} [0.000]"
                    self._log(line)
                    self._log(f"!! {msg}", "response")
                    self._set("print_stats", message=msg)
                    status = "error"
                    break
                x, y = nx, ny
                if "E" in axes and relative and axes["E"] > 0:
                    used += axes["E"]
                self._set("gcode_move", gcode_position=[x, y, axes.get("Z", 0.0), 0.0])
        state = {"completed": "complete", "cancelled": "cancelled", "error": "error"}[
            status
        ]
        if self.state["webhooks"]["state"] != "ready":
            state = "error"
        self._set("virtual_sdcard", is_active=False, progress=1.0 if state == "complete" else
                  self.state["virtual_sdcard"]["progress"])  # fmt: skip
        self._set(
            "print_stats",
            state=state,
            print_duration=duration,
            filament_used=round(used, 2),
        )
        self._set("idle_timeout", state="Idle")
        self._resume.set()
        self.history.insert(
            0,
            {
                "job_id": f"{len(self.history) + 1:06X}", "filename": filename, "status": status,
                "start_time": started, "end_time": _now(), "print_duration": duration,
                "total_duration": _now() - started, "filament_used": round(used, 2),
            },
        )  # fmt: skip
        self._task = None


def camera_jpeg(p: SimPrinter, cam: str) -> bytes:
    """A camera frame: the bed plate, and the part on it once a print has laid a layer, until it is taken off."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 480), (40, 40, 40))
    d = ImageDraw.Draw(img)
    d.rectangle((64, 72, 576, 456), fill=(150, 150, 150))
    if p.part_on_bed:
        ps = p.state["print_stats"]
        info = ps.get("info") or {}
        frac = (info.get("current_layer") or 1) / (info.get("total_layer") or 1)
        size = 60 + 40 * min(1.0, frac)
        d.rectangle(
            (320 - size, 264 - size, 320 + size, 264 + size), fill=(200, 60, 40)
        )
    d.text(
        (8, 8),
        f"{p.name} {cam} {p.state['print_stats']['state']}",
        fill=(255, 255, 255),
    )
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class SimHA:
    """Alerts, notifications and entity states as Home Assistant keeps them, and the taps that come back."""

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []
        self.notifications: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def service(self, name: str, body: dict[str, Any]) -> None:
        self.calls.append((name, body))
        data = body.get("data") or {}
        if not name.startswith("notify."):
            return
        tag = data.get("tag") or f"t{len(self.notifications)}"
        if body.get("message") == "clear_notification":
            self.notifications.pop(tag, None)
            return
        self.notifications[tag] = {
            "tag": tag, "title": body.get("title"), "message": body.get("message"),
            "actions": data.get("actions") or [], "image": data.get("image"), "at": _now(),
        }  # fmt: skip

    def tap(self, action: str, reply_text: str | None = None) -> None:
        data: dict[str, Any] = {"action": action}
        if reply_text is not None:
            data["reply_text"] = reply_text
        for q in list(self.clients):
            q.put_nowait(
                {
                    "type": "event",
                    "event": {
                        "event_type": "mobile_app_notification_action",
                        "data": data,
                    },
                }
            )


def _page(printers: dict[str, SimPrinter], ha: SimHA, token: str | None) -> str:
    e = html.escape
    notes = "".join(
        f"<div class='card'><b>{e(n['title'] or '')}</b><p>{e(n['message'] or '')}</p>"
        + "".join(
            f"<form method='post' action='/sim/tap'><input type='hidden' name='action' value='{e(a['action'])}'>"
            + (
                "<input name='reply_text' placeholder='reply'>"
                if a.get("behavior") == "textInput"
                else ""
            )
            + f"<button>{e(a['title'])}</button></form>"
            for a in n["actions"]
        )
        + "</div>"
        for n in sorted(ha.notifications.values(), key=lambda n: -n["at"])
    )
    alerts = "".join(
        f"<li><b>{e(a.get('severity', ''))}</b> {e(a.get('printer', ''))}: {e(a.get('title', ''))} {e(a.get('message', ''))}</li>"
        for a in reversed(ha.alerts[-15:])
    )
    rows = ""
    for name, p in printers.items():
        ps = p.state["print_stats"]
        rows += (
            f"<div class='card'><b>{e(name)}</b> {e(ps['state'])} "
            f"layer {ps['info']['current_layer']}/{ps['info']['total_layer']} · Klipper {e(p.state['webhooks']['state'])} · "
            f"nozzle → {p.state['extruder']['target']:.0f} °C, bed → {p.state['heater_bed']['target']:.0f} °C"
            f"<form method='post' action='/sim/gcode'><input type='hidden' name='printer' value='{e(name)}'>"
            "<input name='script' placeholder='PAUSE, M112, FIRMWARE_RESTART …'><button>Send</button></form>"
            + (
                f"<form method='post' action='/sim/bed'><input type='hidden' name='printer' value='{e(name)}'>"
                "<button>Take the part off</button></form>"
                if p.part_on_bed and not p.printing
                else ""
            )
            + f"<br><img src='/sim/camera/{e(name)}/bed.jpg' width='320'></div>"
        )
    status = f"/status?token={e(token)}" if token else "/status"
    return (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='refresh' content='5'><title>Demo phone</title>"
        "<style>body{font:15px system-ui;margin:16px;max-width:640px}.card{border:1px solid #ccc;border-radius:8px;"
        "padding:8px;margin:8px 0}form{display:inline-block;margin:2px}</style>"
        f"<h1>Demo phone</h1><p>The warden's own pages: <a href='{status}'>status</a> on the warden's port.</p>"
        f"<h2>Notifications</h2>{notes or '<p>none</p>'}<h2>Printers</h2>{rows}<h2>Alerts</h2><ul>{alerts}</ul>"
    )


def app(
    printers: dict[str, SimPrinter], ha: SimHA, token: str | None = None
) -> Starlette:
    """Moonraker under `/moonraker/<printer>`, Home Assistant under `/api`, the phone under `/sim`."""

    def ok(result: Any) -> JSONResponse:
        return JSONResponse({"result": result})

    async def moonraker(request: Request) -> Response:
        p = printers.get(request.path_params["printer"])
        if p is None:
            return JSONResponse({"error": "no such printer"}, status_code=404)
        path = request.path_params["path"]
        q = request.query_params
        if path == "printer/objects/list":
            return ok({"objects": sorted(p.state)})
        if path == "printer/objects/query":
            return ok({"eventtime": _now(), "status": p.query(k for k in q)})
        if path == "printer/info":
            return ok(
                {"state": p.state["webhooks"]["state"], "software_version": KLIPPER}
            )
        if path == "server/info":
            return ok({"klippy_state": p.state["webhooks"]["state"]})
        if path == "machine/update/status":
            return ok({"version_info": {}})
        if path == "server/history/list":
            return ok({"jobs": p.history[: int(q.get("limit", 50))]})
        if path == "server/gcode_store":
            return ok({"gcode_store": p.store[-int(q.get("count", 40)) :]})
        if path == "machine/device_power/device":
            return ok({q.get("device", "printer"): "on"})
        if path == "server/files/directory":
            return ok("ok")
        if path == "server/files/upload":
            form = await request.form()
            f = form["file"]
            sub = str(form.get("path") or "")
            name = f"{sub}/{f.filename}" if sub else str(f.filename)  # type: ignore[union-attr]
            p.files[name] = (await f.read()).decode(errors="ignore")  # type: ignore[union-attr]
            return JSONResponse(
                {"item": {"path": name, "root": "gcodes"}, "action": "create_file"}
            )
        try:
            if path == "printer/print/start":
                p.start(q["filename"])
            elif path == "printer/print/pause":
                p.pause()
            elif path == "printer/print/resume":
                p.resume()
            elif path == "printer/print/cancel":
                p.cancel()
            elif path == "printer/gcode/script":
                p.script(q["script"])
            elif path == "printer/firmware_restart":
                p.restart()
            else:
                return JSONResponse(
                    {"error": f"{path} is not simulated"}, status_code=404
                )
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
        return ok("ok")

    async def moonraker_ws(ws: WebSocket) -> None:
        p = printers.get(ws.path_params["printer"])
        if p is None:
            await ws.close()
            return
        await ws.accept()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        p.clients.add(q)

        async def pump() -> None:
            while True:
                await ws.send_text(json.dumps(await q.get()))

        sender = asyncio.create_task(pump())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("method") == "printer.objects.subscribe":
                    objs = (msg.get("params") or {}).get("objects") or {}
                    q.put_nowait({"jsonrpc": "2.0", "id": msg.get("id"),
                                  "result": {"eventtime": _now(), "status": p.query(objs)}})  # fmt: skip
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            p.clients.discard(q)

    async def api_root(_: Request) -> Response:
        return JSONResponse({"message": "API running."})

    async def services(_: Request) -> Response:
        return JSONResponse([{"domain": "notify", "services": {"mobile_app_demo": {}}}])

    async def webhook(request: Request) -> Response:
        ha.alerts.append(await request.json())
        return JSONResponse({})

    async def service(request: Request) -> Response:
        ha.service(
            f"{request.path_params['domain']}.{request.path_params['service']}",
            await request.json(),
        )
        return JSONResponse([])

    async def states(request: Request) -> Response:
        entity = request.path_params["entity"]
        if request.method == "POST":
            body = await request.json()
            ha.states[entity] = {"entity_id": entity, **body}
            return JSONResponse(ha.states[entity])
        if entity not in ha.states:
            return JSONResponse({"message": "Entity not found."}, status_code=404)
        return JSONResponse(ha.states[entity])

    async def ha_ws(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        ha.clients.add(q)

        async def pump() -> None:
            while True:
                await ws.send_json(await q.get())

        sender = asyncio.create_task(pump())
        try:
            while True:
                msg = await ws.receive_json()
                q.put_nowait({"id": msg.get("id"), "type": "result", "success": True})
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            ha.clients.discard(q)

    async def phone(_: Request) -> Response:
        return HTMLResponse(_page(printers, ha, token))

    async def tap(request: Request) -> Response:
        form = await request.form()
        ha.tap(
            str(form["action"]),
            str(form["reply_text"]) if form.get("reply_text") else None,
        )
        return _back(request)

    async def gcode(request: Request) -> Response:
        form = await request.form()
        p = printers.get(str(form["printer"]))
        if p is None:
            return JSONResponse({"error": "no such printer"}, status_code=404)
        p.script(str(form["script"]))
        return _back(request)

    def _back(request: Request) -> Response:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": True})
        return RedirectResponse("/sim/phone", status_code=303)

    async def camera(request: Request) -> Response:
        p = printers.get(request.path_params["printer"])
        if p is None:
            return JSONResponse({"error": "no such printer"}, status_code=404)
        return Response(
            camera_jpeg(p, request.path_params["cam"]), media_type="image/jpeg"
        )

    async def bed(request: Request) -> Response:
        form = await request.form()
        p = printers.get(str(form["printer"]))
        if p is None:
            return JSONResponse({"error": "no such printer"}, status_code=404)
        if not p.printing:
            p.part_on_bed = False
        return _back(request)

    async def notifications(_: Request) -> Response:
        return JSONResponse(
            {
                "notifications": sorted(
                    ha.notifications.values(), key=lambda n: -n["at"]
                ),
                "alerts": ha.alerts,
                "printers": {
                    n: p.query(["print_stats", "webhooks", "extruder", "heater_bed"])
                    for n, p in printers.items()
                },
            }
        )

    return Starlette(
        routes=[
            Route(
                "/moonraker/{printer}/{path:path}", moonraker, methods=["GET", "POST"]
            ),
            WebSocketRoute("/moonraker/{printer}/websocket", moonraker_ws),
            Route("/api/", api_root),
            Route("/api/services", services),
            Route("/api/webhook/{hook}", webhook, methods=["POST"]),
            Route("/api/services/{domain}/{service}", service, methods=["POST"]),
            Route("/api/states/{entity}", states, methods=["GET", "POST"]),
            WebSocketRoute("/api/websocket", ha_ws),
            Route("/sim/phone", phone),
            Route("/sim/tap", tap, methods=["POST"]),
            Route("/sim/gcode", gcode, methods=["POST"]),
            Route("/sim/notifications", notifications),
            Route("/sim/camera/{printer}/{cam}.jpg", camera),
            Route("/sim/bed", bed, methods=["POST"]),
        ]
    )


def slice_to_3mf(
    stl: Path,
    threemf: Path,
    machine: dict[str, Any],
    process: dict[str, Any],
    filament: dict[str, Any],
    copies: int = 1,
) -> str:
    """The `orca: sim` slicer: a 3MF holding a gcode for the model's bounding box, centred on the bed. Returns its log."""
    from .checks import bed_rect, stl_bounds

    dx, dy, dz = stl_bounds(stl)
    x0, y0, x1, y1 = bed_rect(machine)
    w, d = dx * copies + 5 * (copies - 1), dy
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ax, ay, bx, by = cx - w / 2, cy - d / 2, cx + w / 2, cy + d / 2
    h = float(_first(process.get("layer_height"), 0.2))
    nozzle = float(_first(filament.get("nozzle_temperature"), 210))
    bed = float(_first(filament.get("hot_plate_temp"), 60))
    layers = max(1, round(dz / h))
    perim = 2 * (w + d)
    e_layer = perim * 0.033
    grams = round(e_layer * layers * 2.98e-3, 2)
    est = round(layers * LAYER_S)
    out = [
        "; generated by print-warden's simulated slicer",
        f"; estimated printing time (normal mode) = {est // 60}m {est % 60}s",
        f"; total layer number: {layers}",
        f"; max_z_height: {layers * h:.2f}",
        f"; filament used [mm] = {e_layer * layers:.2f}",
        f"; filament used [g] = {grams}",
        ";TYPE:Custom",
        f"M140 S{bed:.0f}",
        f"M104 S{nozzle:.0f}",
        "G28",
        f"M190 S{bed:.0f}",
        f"M109 S{nozzle:.0f}",
        "G90",
        "M83",
    ]
    for i in range(1, layers + 1):
        z = round(i * h, 3)
        out += [
            ";LAYER_CHANGE",
            f";Z:{z}",
            f"G1 Z{z} F600",
            ";TYPE:Outer wall",
            f"G1 X{ax:.2f} Y{ay:.2f} F6000",
            f"G1 X{bx:.2f} Y{ay:.2f} E{e_layer * w / perim:.4f}",
            f"G1 X{bx:.2f} Y{by:.2f} E{e_layer * d / perim:.4f}",
            f"G1 X{ax:.2f} Y{by:.2f} E{e_layer * w / perim:.4f}",
            f"G1 X{ax:.2f} Y{ay:.2f} E{e_layer * d / perim:.4f}",
        ]
    out += [
        ";TYPE:Custom",
        *str(machine.get("machine_end_gcode") or "M104 S0\nM140 S0\nM84").splitlines(),
    ]
    threemf.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", "\n".join(out) + "\n")
    return f"simulated slice: {layers} layers of {h} mm, {grams} g\n"


def _first(v: Any, default: float) -> Any:
    if isinstance(v, list):
        v = v[0] if v else None
    return v if v not in (None, "") else default
