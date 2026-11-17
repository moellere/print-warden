"""`print-warden record`: one print as a real printer reported it, for the simulator to replay in tests.

    print-warden record --printer P [--out recording.jsonl] [--minutes 180]

It subscribes to the printer's Moonraker the way the warden does (read-only: no gcode, no file, no start), waits
for a print to start, and writes every status update and Klipper signal until the print ends (or `--minutes` pass),
then Moonraker's gcode store and the history entry. Start it, then start the print: the fault test, a cancel, an
`M112`, a normal cube.

The file is JSON lines: a `meta` line (printer name, Klipper version, when), a `snapshot` of every subscribed object,
then `status` and `signal` lines with `t` seconds from the start, then `gcode_store` and `history`. File names are
replaced with `recorded-<n>.gcode` (a `warden/` prefix is kept), so a recording carries no job or model names.
`sim.SimPrinter(replay=...)` plays it back when any print starts (`print-warden demo --replay demo-a=FILE`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets

from . import config, moonraker
from .rules import watched_object

FINAL = ("complete", "cancelled", "error")


class Anonymizer:
    """The same file name always maps to the same `recorded-<n>.gcode`."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def name(self, value: str) -> str:
        if not value:
            return value
        if value not in self.names:
            prefix = "warden/" if value.startswith("warden/") else ""
            self.names[value] = f"{prefix}recorded-{len(self.names) + 1}.gcode"
        return self.names[value]

    def status(self, objs: dict[str, Any]) -> dict[str, Any]:
        out = json.loads(json.dumps(objs))
        ps = out.get("print_stats")
        if isinstance(ps, dict) and ps.get("filename"):
            ps["filename"] = self.name(ps["filename"])
        vs = out.get("virtual_sdcard")
        if isinstance(vs, dict) and vs.get("file_path"):
            vs["file_path"] = self.name(vs["file_path"])
        return out

    def text(self, s: str) -> str:
        for real, fake in self.names.items():
            s = s.replace(real, fake)
            s = s.replace(real.rsplit("/", 1)[-1], fake.rsplit("/", 1)[-1])
        return s


async def capture(
    messages: AsyncIterator[dict[str, Any]],
    snapshot: dict[str, Any],
    write: Callable[[dict[str, Any]], None],
    fetch_store: Callable[[], Awaitable[list[dict[str, Any]]]],
    fetch_history: Callable[[], Awaitable[list[dict[str, Any]]]],
    meta: dict[str, Any],
    minutes: float = 180,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Write one print from `messages` (Moonraker's websocket, decoded); returns how the print ended."""
    anon = Anonymizer()
    state = (snapshot.get("print_stats") or {}).get("state")
    started = state == "printing"
    t0 = clock()
    write({"kind": "meta", **meta})
    write({"kind": "snapshot", "data": anon.status(snapshot)})
    ended = "timeout"
    async for msg in messages:
        if clock() - t0 > minutes * 60:
            break
        method = msg.get("method")
        if method == "notify_status_update":
            changed = msg["params"][0]
            new = (changed.get("print_stats") or {}).get("state")
            if new == "printing":
                started = True
            write(
                {
                    "kind": "status",
                    "t": round(clock() - t0, 3),
                    "data": anon.status(changed),
                }
            )
            if started and new in FINAL:
                ended = new
                break
        elif method in (
            "notify_klippy_shutdown",
            "notify_klippy_ready",
            "notify_klippy_disconnected",
        ):
            write({"kind": "signal", "t": round(clock() - t0, 3), "method": method})
            if started and method == "notify_klippy_shutdown":
                ended = "shutdown"
                break
    store = await fetch_store()
    write(
        {
            "kind": "gcode_store",
            "data": [
                {**e, "message": anon.text(str(e.get("message", "")))} for e in store
            ],
        }
    )
    hist = await fetch_history()
    write(
        {
            "kind": "history",
            "data": [
                {**h, "filename": anon.name(str(h.get("filename") or ""))}
                for h in hist[:1]
            ],
        }
    )
    return ended


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _record(p: config.Printer, out: Path, minutes: float) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        names = (await c.get(f"{p.moonraker}/printer/objects/list")).json()["result"][
            "objects"
        ]
        info = (await c.get(f"{p.moonraker}/printer/info")).json()["result"]
    objs: dict[str, Any] = dict(moonraker.SUBSCRIBE)
    objs.update({n: None for n in names if watched_object(n)})
    objs.update(
        {n: None for n in names if n in ("configfile", "bed_mesh") or n == "mcu"}
    )
    with out.open("w") as f:

        def write(line: dict[str, Any]) -> None:
            f.write(json.dumps(line) + "\n")
            f.flush()

        async with websockets.connect(p.ws_url, max_size=16 * 1024 * 1024) as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "printer.objects.subscribe",
                        "params": {"objects": objs},
                        "id": 1,
                    }
                )
            )
            snapshot: dict[str, Any] = {}
            while not snapshot:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    snapshot = (msg.get("result") or {}).get("status") or {}

            async def messages() -> AsyncIterator[dict[str, Any]]:
                async for raw in ws:
                    yield json.loads(raw)

            meta = {
                "printer": p.name,
                "klipper": info.get("software_version"),
                "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            print(
                f"recording {p.name} to {out}: start the print now (Ctrl-C stops)",
                flush=True,
            )
            return await capture(
                messages(),
                snapshot,
                write,
                lambda: moonraker.gcode_store(p.moonraker, 200),
                lambda: moonraker.history(p.moonraker, 1),
                meta,
                minutes,
            )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="print-warden record", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "--config", default=os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    )
    ap.add_argument("--printer", required=True)
    ap.add_argument("--out", help="default <printer>-<time>.jsonl")
    ap.add_argument("--minutes", type=float, default=180)
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if args.printer not in cfg.printers:
        print(f"unknown printer {args.printer}; one of {', '.join(cfg.printers)}")
        return 2
    out = Path(args.out or f"{args.printer}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        ended = asyncio.run(_record(cfg.printers[args.printer], out, args.minutes))
    except KeyboardInterrupt:
        print(
            f"\nstopped; {out} holds what was recorded so far (no gcode store or history)"
        )
        return 1
    except (
        httpx.HTTPError,
        OSError,
        websockets.WebSocketException,
        KeyError,
        ValueError,
    ) as e:
        print(f"cannot record {args.printer}: {e}")
        return 1
    print(f"{out}: the print ended {ended}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
