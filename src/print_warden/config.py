"""Configuration loading. YAML in, frozen dataclasses out; env vars hold secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Rules:
    heater_deviation_c: float = 8.0
    heater_deviation_s: float = 90.0
    stall_minutes: float = 10.0
    layer_snapshot: bool = True


@dataclass(frozen=True)
class SlicerConfig:
    orca: str = "/opt/orcaslicer/AppRun"
    openscad: str = "openscad"
    vendor_dir: str = "/opt/orcaslicer/resources/profiles/Artillery"
    house_rules: str | None = None


@dataclass(frozen=True)
class Printer:
    name: str
    moonraker: str
    cameras: dict[str, str]
    rotate: dict[str, int] = field(default_factory=dict)
    flip_v: dict[str, bool] = field(default_factory=dict)
    light: str | None = None
    rules: Rules = Rules()
    machine: str | None = None
    processes: dict[str, str] = field(default_factory=dict)
    filaments: dict[str, str] = field(default_factory=dict)
    auto_start: bool = False
    location: str | None = None
    power_device: str | None = None
    track_spool_usage: bool = True

    @property
    def ws_url(self) -> str:
        return (
            self.moonraker.replace("http://", "ws://")
            .replace("https://", "wss://")
            .rstrip("/")
            + "/websocket"
        )


@dataclass(frozen=True)
class HomeAssistant:
    url: str
    webhook_id: str
    token: str | None
    verify_tls: bool = False
    notify_service: str | None = None
    action_secret: str | None = None


@dataclass(frozen=True)
class Config:
    printers: dict[str, Printer]
    ha: HomeAssistant | None
    obico_url: str | None
    obico_webhook_secret: str | None
    storage_dir: Path
    keep_days: int
    host: str
    port: int
    public_url: str
    slicer: SlicerConfig = SlicerConfig()
    spoolman_url: str | None = None
    api_token: str | None = None
    approval_ttl_minutes: float = 15.0


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    printers: dict[str, Printer] = {}
    for name, p in (raw.get("printers") or {}).items():
        printers[name] = Printer(
            name=name,
            moonraker=p["moonraker"].rstrip("/"),
            cameras=dict(p.get("cameras") or {}),
            rotate={k: int(v) for k, v in (p.get("rotate") or {}).items()},
            flip_v={k: bool(v) for k, v in (p.get("flip_v") or {}).items()},
            light=p.get("light"),
            rules=Rules(**(p.get("rules") or {})),
            machine=(p.get("slicer") or {}).get("machine"),
            processes={
                k.lower(): v
                for k, v in ((p.get("slicer") or {}).get("processes") or {}).items()
            },
            filaments={
                k.upper(): v
                for k, v in ((p.get("slicer") or {}).get("filaments") or {}).items()
            },
            auto_start=bool(p.get("auto_start", False)),
            location=(p.get("location") or None),
            power_device=(p.get("power_device") or None),
            track_spool_usage=bool(p.get("track_spool_usage", True)),
        )
    ha = None
    if h := raw.get("home_assistant"):
        ha = HomeAssistant(
            url=h["url"].rstrip("/"),
            webhook_id=h["webhook_id"],
            token=os.environ.get(h.get("token_env", "HA_BEARER_TOKEN")),
            verify_tls=bool(h.get("verify_tls", False)),
            notify_service=h.get("notify_service"),
            action_secret=os.environ.get(
                h.get("action_secret_env", "HA_ACTION_SECRET")
            ),
        )
    ob = raw.get("obico") or {}
    st = raw.get("storage") or {}
    sv = raw.get("server") or {}
    pol = raw.get("policy") or {}
    host = sv.get("host", "0.0.0.0")
    port = int(sv.get("port", 8710))
    return Config(
        printers=printers,
        ha=ha,
        obico_url=(ob.get("url") or "").rstrip("/") or None,
        obico_webhook_secret=os.environ.get(
            ob.get("webhook_secret_env", "OBICO_WEBHOOK_SECRET")
        ),
        storage_dir=Path(st.get("dir", "./data")),
        keep_days=int(st.get("keep_days", 14)),
        host=host,
        port=port,
        public_url=(sv.get("public_url") or f"http://{host}:{port}").rstrip("/"),
        slicer=SlicerConfig(**(raw.get("slicer") or {})),
        spoolman_url=((raw.get("spoolman") or {}).get("url") or "").rstrip("/") or None,
        api_token=os.environ.get(sv.get("token_env", "PRINT_WARDEN_TOKEN")) or None,
        approval_ttl_minutes=float(pol.get("approval_ttl_minutes", 15)),
    )
