"""Home Assistant entities over MQTT discovery, as an alternative to the REST states API (`ha_entities.py`).

Same sensors, same entity ids (`sensor.print_warden_<printer>_<key>`), but each has a `unique_id` and belongs to one
device per printer, so it can be renamed, given an area and shown on a device page. Discovery configs, states and
attributes are retained on the broker, so Home Assistant restores them after a restart without a republish; a retained
`print_warden/status` ("offline" on shutdown, and as the last will if the connection drops) marks every entity
unavailable while the warden is down.

The publisher only writes topics under `print_warden/` and the discovery prefix; it subscribes to nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from .config import Mqtt
from .ha_entities import Row

log = logging.getLogger(__name__)

ROOT = "print_warden"
AVAILABILITY = f"{ROOT}/status"
CONFIG_KEYS = ("unit_of_measurement", "device_class", "icon")


class Publisher(Protocol):
    async def publish(self, topic: str, payload: str, *, retain: bool) -> None: ...


def _key(entity_id: str, slug: str) -> str:
    return entity_id.removeprefix(f"sensor.{ROOT}_{slug}_")


def messages(
    printer: str, slug: str, rows: list[Row], prefix: str, version: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(discovery messages, state messages) for one printer's rows; every message is retained."""
    device = {
        "identifiers": [f"{ROOT}_{slug}"],
        "name": printer,
        "manufacturer": "print-warden",
        "model": "Klipper printer",
        "sw_version": version,
    }
    discovery: list[tuple[str, str]] = []
    states: list[tuple[str, str]] = []
    for entity_id, state, attrs in rows:
        key = _key(entity_id, slug)
        object_id = f"{ROOT}_{slug}_{key}"
        base = f"{ROOT}/{slug}/{key}"
        config: dict[str, Any] = {
            "name": key.replace("_", " ").capitalize(),
            "unique_id": object_id,
            "default_entity_id": f"sensor.{object_id}",
            "state_topic": f"{base}/state",
            "json_attributes_topic": f"{base}/attributes",
            "availability_topic": AVAILABILITY,
            "device": device,
        }
        config.update({k: attrs[k] for k in CONFIG_KEYS if k in attrs})
        discovery.append(
            (f"{prefix}/sensor/{object_id}/config", json.dumps(config, sort_keys=True))
        )
        if state == "unknown" and attrs.get("device_class") == "timestamp":
            state = "None"
        extra = {
            k: v
            for k, v in attrs.items()
            if k not in CONFIG_KEYS and k != "friendly_name"
        }
        states.append((f"{base}/state", state))
        states.append((f"{base}/attributes", json.dumps(extra, sort_keys=True)))
    return discovery, states


class MqttEntities:
    """Publishes what changed since the last pass; a fresh connection republishes everything."""

    def __init__(self, cfg: Mqtt, version: str):
        self.cfg = cfg
        self.version = version
        self.sent: dict[str, str] = {}

    async def publish(
        self, client: Publisher, printers: dict[str, tuple[str, list[Row]]]
    ) -> int:
        n = 0
        for printer, (slug, rows) in printers.items():
            discovery, states = messages(
                printer, slug, rows, self.cfg.discovery_prefix, self.version
            )
            for topic, payload in discovery + states:
                if self.sent.get(topic) == payload:
                    continue
                await client.publish(topic, payload, retain=True)
                self.sent[topic] = payload
                n += 1
        return n

    async def run(
        self,
        snapshot: Callable[[], dict[str, tuple[str, list[Row]]]],
        interval_s: float = 30,
    ) -> None:
        import aiomqtt

        while True:
            try:
                async with aiomqtt.Client(
                    self.cfg.host,
                    self.cfg.port,
                    username=self.cfg.username,
                    password=self.cfg.password,
                    identifier=f"{ROOT}-{self.cfg.host}",
                    will=aiomqtt.Will(AVAILABILITY, "offline", retain=True),
                ) as client:
                    self.sent.clear()
                    await client.publish(AVAILABILITY, "online", retain=True)
                    try:
                        while True:
                            await self.publish(client, snapshot())
                            await asyncio.sleep(interval_s)
                    except asyncio.CancelledError:
                        await client.publish(AVAILABILITY, "offline", retain=True)
                        raise
            except aiomqtt.MqttError as e:
                log.warning(
                    "mqtt %s:%s: %s; retrying in 30 s", self.cfg.host, self.cfg.port, e
                )
                await asyncio.sleep(30)
