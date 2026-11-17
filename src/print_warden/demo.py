"""`print-warden demo`: the whole warden against two simulated printers, a simulated phone and a simulated slicer.

    print-warden demo [--dir DIR] [--port 8710] [--sim-port 8711] [--speed 12] [--pause-cancel-minutes 2]

It writes `DIR/printers.yaml` (two printers, `demo-a` and `demo-b`, in one location), a slicer profile set and the
storage under DIR, then serves the simulation (`sim.py`) on `--sim-port` and the warden on `--port`. The phone is
`http://127.0.0.1:<sim-port>/sim/phone`: the warden's notifications with their buttons, the alerts, and a console
per printer. Everything the warden does is real; only the printers, Home Assistant and OrcaSlicer are simulated.
Prints, grace periods and the paused-print deadline run `--speed` times faster than real time.

`print-warden validate --config DIR/printers.yaml` rehearses the live-validation runbook against it. The API token
is `demo` unless PRINT_WARDEN_TOKEN is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn
import yaml

PRINTERS = ("demo-a", "demo-b")
MACHINE = "Sim 220 0.4 nozzle"
PROCESSES = {
    "draft": ("0.24mm Draft @Sim", 0.24),
    "standard": ("0.20mm Standard @Sim", 0.2),
}
FILAMENTS = {"PLA": ("Sim PLA", 210, 60), "PETG": ("Sim PETG", 240, 80)}


def write(
    root: Path, port: int, sim_port: int, speed: float, pause_cancel_min: float
) -> Path:
    """The demo's config and slicer profiles; returns the config path."""
    vendor = root / "profiles" / "Sim"
    presets: dict[str, list[dict[str, Any]]] = {
        "machine": [
            {
                "name": MACHINE, "printable_area": ["0x0", "220x0", "220x220", "0x220"],
                "printable_height": "250", "nozzle_diameter": ["0.4"],
                "machine_end_gcode": "M104 S0\nM140 S0\nG91\nG1 Z10\nG90\nM84",
            }
        ],
        "process": [{"name": n, "layer_height": str(h)} for n, h in PROCESSES.values()],
        "filament": [
            {"name": n, "nozzle_temperature": [str(t)], "hot_plate_temp": [str(b)]} for n, t, b in FILAMENTS.values()
        ],
    }  # fmt: skip
    for kind, items in presets.items():
        (vendor / kind).mkdir(parents=True, exist_ok=True)
        for p in items:
            (vendor / kind / f"{p['name']}.json").write_text(json.dumps(p))
    sim = f"http://127.0.0.1:{sim_port}"
    cfg = {
        "printers": {
            name: {
                "location": "lab",
                "moonraker": f"{sim}/moonraker/{name}",
                "cameras": {
                    c: f"{sim}/sim/camera/{name}/{c}.jpg" for c in ("bed", "nozzle")
                },
                "bed_check": {
                    "camera": "bed",
                    "region": [0.1, 0.15, 0.9, 0.95],
                    "threshold": 0.002,
                    "min_agreements": 3,
                },
                "slicer": {
                    "machine": MACHINE,
                    "processes": {q: n for q, (n, _) in PROCESSES.items()},
                    "filaments": {m: n for m, (n, _, _) in FILAMENTS.items()},
                },
                "maintenance": {"nozzle": {"every_hours": 300}},
            }
            for name in PRINTERS
        },
        "home_assistant": {
            "url": sim,
            "webhook_id": "demo",
            "token_env": "PRINT_WARDEN_TOKEN",
            "notify_service": "notify.mobile_app_demo",
            "publish_entities": True,
        },
        "slicer": {
            "orca": "sim",
            "vendor_dir": str(vendor),
            "profiles_root": str(root / "profiles"),
        },
        "storage": {"dir": str(root / "data"), "keep_days": 14},
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "token_env": "PRINT_WARDEN_TOKEN",
        },
        "policy": {
            "approval_ttl_minutes": 15,
            "paused_cancel_minutes": pause_cancel_min,
        },
        "alerts": {"cooldown_s": 0},
        "upkeep": {"reorder_below_g": 0},
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "printers.yaml"
    path.write_text(
        f"# print-warden demo: simulated printers at {sim}, {speed:g}x real time. Rewritten on each start.\n"
        + yaml.safe_dump(cfg, sort_keys=False)
    )
    return path


async def serve(
    root: Path, port: int, sim_port: int, speed: float, pause_cancel_min: float
) -> None:
    path = write(root, port, sim_port, speed, pause_cancel_min)
    os.environ["PRINT_WARDEN_CONFIG"] = str(path)
    os.environ.setdefault("PRINT_WARDEN_TOKEN", "demo")
    from . import sim

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    printers = {n: sim.SimPrinter(n, speed=speed) for n in PRINTERS}
    ha = sim.SimHA()
    token = os.environ["PRINT_WARDEN_TOKEN"]
    sim_server = uvicorn.Server(
        uvicorn.Config(
            sim.app(printers, ha, token),
            host="127.0.0.1",
            port=sim_port,
            log_level="warning",
        )
    )
    sim_task = asyncio.create_task(sim_server.serve())
    while not sim_server.started:
        await asyncio.sleep(0.05)

    from . import server, watcher

    # The warden's own grace periods run at the demo's speed, as the prints do.
    watcher.COOL_GRACE_S /= speed
    watcher.BED_SETTLE_S /= speed
    watcher.PAUSE_POLL_S = min(watcher.PAUSE_POLL_S, 2.0)
    for name in PRINTERS:
        if not server.JOBS.kv_get(f"loaded_material:{name}"):
            server.JOBS.kv_set(f"loaded_material:{name}", "PLA")
    print(
        f"print-warden demo: two simulated printers ({', '.join(PRINTERS)}), {speed:g}x real time\n"
        f"  phone   http://127.0.0.1:{sim_port}/sim/phone\n"
        f"  status  http://127.0.0.1:{port}/status?token={token}\n"
        f"  stage   http://127.0.0.1:{port}/stage?token={token}\n"
        f"  MCP     http://127.0.0.1:{port}/mcp (Authorization: Bearer {token})\n"
        f"  runbook PRINT_WARDEN_TOKEN={token} print-warden validate --config {path} 1 --printer demo-a --setup",
        flush=True,
    )
    warden = uvicorn.Server(
        uvicorn.Config(server.asgi, host="127.0.0.1", port=port, log_level="warning")
    )
    try:
        await warden.serve()
    finally:
        sim_server.should_exit = True
        await sim_task


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="print-warden demo", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "--dir", default="print-warden-demo", help="where the demo's config and data go"
    )
    ap.add_argument("--port", type=int, default=8710, help="the warden")
    ap.add_argument(
        "--sim-port",
        type=int,
        default=8711,
        help="the simulated printers, Home Assistant and phone",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=12.0,
        help="how many times faster than real time prints run",
    )
    ap.add_argument(
        "--pause-cancel-minutes",
        type=float,
        default=2.0,
        help="policy.paused_cancel_minutes (real minutes)",
    )
    args = ap.parse_args(argv)
    if args.speed <= 0:
        ap.error("--speed must be above 0")
    try:
        asyncio.run(
            serve(
                Path(args.dir).resolve(),
                args.port,
                args.sim_port,
                args.speed,
                args.pause_cancel_minutes,
            )
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
