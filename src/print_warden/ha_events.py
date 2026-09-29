"""Home Assistant websocket subscriber: relays notification button taps to a callback. Read-only."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable

import websockets

from .config import HomeAssistant

log = logging.getLogger(__name__)


async def watch_notification_actions(
    cfg: HomeAssistant, on_action: Callable[[str, dict], Awaitable[None]]
) -> None:
    url = (
        cfg.url.replace("https://", "wss://").replace("http://", "ws://")
        + "/api/websocket"
    )
    ctx: ssl.SSLContext | None = None
    if url.startswith("wss://") and not cfg.verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    while True:
        try:
            async with websockets.connect(url, ssl=ctx, ping_interval=30) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": cfg.token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    log.error("HA websocket auth failed")
                    await asyncio.sleep(60)
                    continue
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "type": "subscribe_events",
                            "event_type": "mobile_app_notification_action",
                        }
                    )
                )
                log.info("subscribed to HA notification actions")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    data = msg["event"].get("data", {})
                    action = data.get("action") or ""
                    if action.startswith("PRINT_WARDEN_"):
                        await on_action(action, data)
        except (TimeoutError, OSError, websockets.WebSocketException) as e:
            log.warning("HA websocket: %s", e)
        await asyncio.sleep(10)
