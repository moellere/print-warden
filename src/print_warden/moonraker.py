"""Moonraker websocket client: subscribes to printer objects, merges status updates into one dict,
and reports klippy connection changes. Reconnects forever. Read-only."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets

log = logging.getLogger(__name__)

SUBSCRIBE = {
    "print_stats": None,
    "display_status": None,
    "virtual_sdcard": ["progress", "is_active"],
    "webhooks": None,
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "toolhead": ["homed_axes"],
    "idle_timeout": ["state"],
    "gcode_move": ["gcode_position"],
}

Listener = Callable[[dict[str, Any], str | None], Awaitable[None]]


class MoonrakerClient:
    def __init__(self, ws_url: str, listener: Listener):
        self.ws_url = ws_url
        self.listener = listener
        self.state: dict[str, Any] = {}
        self.connected = False
        self._id = 0

    def _merge(self, update: dict[str, Any]) -> None:
        for obj, fields in update.items():
            if isinstance(fields, dict):
                self.state.setdefault(obj, {}).update(fields)

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, max_size=4 * 1024 * 1024
                ) as ws:
                    self.connected = True
                    log.info("connected %s", self.ws_url)
                    await self._rpc(
                        ws, "printer.objects.subscribe", {"objects": SUBSCRIBE}
                    )
                    async for raw in ws:
                        msg = json.loads(raw)
                        if (
                            "result" in msg
                            and isinstance(msg["result"], dict)
                            and "status" in msg["result"]
                        ):
                            self._merge(msg["result"]["status"])
                            await self.listener(self.state, "snapshot")
                        m = msg.get("method")
                        if m == "notify_status_update":
                            self._merge(msg["params"][0])
                            await self.listener(self.state, None)
                        elif m in (
                            "notify_klippy_disconnected",
                            "notify_klippy_shutdown",
                            "notify_klippy_ready",
                        ):
                            await self.listener(self.state, m)
                            if m == "notify_klippy_ready":
                                await self._rpc(
                                    ws,
                                    "printer.objects.subscribe",
                                    {"objects": SUBSCRIBE},
                                )
            except (TimeoutError, OSError, websockets.WebSocketException) as e:
                log.warning("moonraker %s: %s", self.ws_url, e)
            finally:
                if self.connected:
                    self.connected = False
                    await self.listener(self.state, "transport_lost")
            await asyncio.sleep(5)

    async def _rpc(self, ws: Any, method: str, params: dict[str, Any]) -> None:
        self._id += 1
        await ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
            )
        )


async def upload_gcode(base_url: str, gcode: Path, remote_name: str) -> dict[str, Any]:
    """Stage a gcode file on the printer (root=gcodes). Does not start it. Creates the subdirectory if needed."""
    subdir, _, base = remote_name.rpartition("/")
    async with httpx.AsyncClient(timeout=120) as c:
        if subdir:
            await c.post(
                f"{base_url}/server/files/directory",
                params={"path": f"gcodes/{subdir}"},
            )
        with gcode.open("rb") as f:
            r = await c.post(
                f"{base_url}/server/files/upload",
                data={"root": "gcodes", "path": subdir},
                files={"file": (base, f, "text/plain")},
            )
        r.raise_for_status()
        return r.json()


async def start_print(base_url: str, remote_name: str) -> dict[str, Any]:
    """Start a staged gcode file. Callers must run policy.can_start first."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{base_url}/printer/print/start", params={"filename": remote_name}
        )
        r.raise_for_status()
        return r.json()


async def power(base_url: str, device: str, action: str | None = None) -> str:
    """Query (action=None) or switch a Moonraker power device. Returns the device state."""
    async with httpx.AsyncClient(timeout=15) as c:
        if action:
            r = await c.post(
                f"{base_url}/machine/device_power/device",
                params={"device": device, "action": action},
            )
        else:
            r = await c.get(
                f"{base_url}/machine/device_power/device", params={"device": device}
            )
        r.raise_for_status()
        return str(r.json().get("result", {}).get(device, "unknown"))


async def klippy_state(base_url: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{base_url}/server/info")
        r.raise_for_status()
        return str(r.json().get("result", {}).get("klippy_state", "unknown"))


async def firmware_restart(base_url: str) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        (await c.post(f"{base_url}/printer/firmware_restart")).raise_for_status()


async def bring_up(
    base_url: str, device: str, timeout_s: float = 100.0, max_restarts: int = 3
) -> tuple[bool, list[str]]:
    """Power the printer on and coax Klipper to ready. After USB-backfed MCU comm loss the first FIRMWARE_RESTART
    only reconnects (state -> error) and a second one resets it; this loop issues up to `max_restarts`."""
    steps: list[str] = []
    t0 = asyncio.get_event_loop().time()
    steps.append(f"power {device}: {await power(base_url, device, 'on')}")
    last_restart = t0 - 100
    restarts = 0
    while asyncio.get_event_loop().time() - t0 < timeout_s:
        await asyncio.sleep(5)
        st = await klippy_state(base_url)
        now = asyncio.get_event_loop().time()
        if st == "ready":
            steps.append(f"klippy ready after {now - t0:.0f}s")
            return True, steps
        if (
            st in ("shutdown", "error", "disconnected")
            and now - last_restart >= 12
            and restarts < max_restarts
        ):
            await firmware_restart(base_url)
            restarts += 1
            last_restart = now
            steps.append(
                f"FIRMWARE_RESTART #{restarts} at {now - t0:.0f}s (klippy was {st})"
            )
    steps.append(
        f"gave up after {timeout_s:.0f}s, klippy {await klippy_state(base_url)}"
    )
    return False, steps
