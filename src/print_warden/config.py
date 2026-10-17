"""Configuration loading. YAML in, frozen dataclasses out; env vars hold secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .upkeep import Interval, parse_maintenance


@dataclass(frozen=True)
class Rules:
    heater_deviation_c: float = 8.0
    heater_deviation_s: float = 90.0
    stall_minutes: float = 10.0
    layer_snapshot: bool = True
    fan_stall_s: float = 10.0
    fan_min_rpm: float = 100.0
    first_layer_check: int = 2
    temp_limits: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Pose:
    """Where the bed and gantry sit for every bed check: Y (and optionally X) exactly, Z at least this high."""

    y: float
    z: float
    x: float | None = None


@dataclass(frozen=True)
class BedCheck:
    camera: str = "bed"
    region: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    threshold: float = 0.002
    min_agreements: int = 10
    pose: Pose | None = None
    rehome_y: bool = False


@dataclass(frozen=True)
class SlicerConfig:
    orca: str = "/opt/orcaslicer/AppRun"
    openscad: str = "openscad"
    vendor_dir: str = "/opt/orcaslicer/resources/profiles/Artillery"
    house_rules: str | None = None
    blocking_warnings: tuple[str, ...] = (
        "empty layer",
        "floating",
        "out of the plate",
        "outside the plate",
    )


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
    bed_check: BedCheck | None = None
    auto_start_hours: str | None = None
    finish_window: str | None = None
    maintenance: dict[str, Interval] = field(default_factory=dict)

    @property
    def ws_url(self) -> str:
        return (
            self.moonraker.replace("http://", "ws://")
            .replace("https://", "wss://")
            .rstrip("/")
            + "/websocket"
        )


@dataclass(frozen=True)
class Upkeep:
    reorder_below_g: float = 200.0
    dry_after_days: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Mqtt:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"


@dataclass(frozen=True)
class HomeAssistant:
    url: str
    webhook_id: str
    token: str | None
    verify_tls: bool = False
    notify_service: str | None = None
    action_secret: str | None = None
    publish_entities: bool = True
    mqtt: Mqtt | None = None


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
    quiet_hours: str | None = None
    alert_cooldown_s: float = 300.0
    quiet_entity: str | None = None
    quiet_states: tuple[str, ...] = ("on",)
    upkeep: Upkeep = Upkeep()


def _mqtt(raw: dict[str, Any] | None) -> Mqtt | None:
    if not raw:
        return None
    return Mqtt(
        host=raw["host"],
        port=int(raw.get("port", 1883)),
        username=raw.get("username"),
        password=os.environ.get(raw.get("password_env", "MQTT_PASSWORD")) or None,
        discovery_prefix=str(raw.get("discovery_prefix", "homeassistant")).rstrip("/"),
    )


def _bed_check(raw: dict[str, Any] | None) -> BedCheck | None:
    if not raw:
        return None
    region = tuple(float(v) for v in raw.get("region", (0, 0, 1, 1)))
    if len(region) != 4 or not (
        0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1
    ):
        raise ValueError(
            f"bed_check.region must be [x0, y0, x1, y1] fractions, got {region}"
        )
    pose = None
    if pr := raw.get("pose"):
        pose = Pose(
            y=float(pr["y"]),
            z=float(pr["z"]),
            x=float(pr["x"]) if pr.get("x") is not None else None,
        )
    rehome = bool(raw.get("rehome_y", False))
    if rehome and not pose:
        raise ValueError("bed_check.rehome_y needs bed_check.pose")
    return BedCheck(
        camera=str(raw.get("camera", "bed")),
        region=(region[0], region[1], region[2], region[3]),
        threshold=float(raw.get("threshold", 0.002)),
        min_agreements=int(raw.get("min_agreements", 10)),
        pose=pose,
        rehome_y=rehome,
    )


def _slicer(raw: dict[str, Any]) -> SlicerConfig:
    raw = dict(raw)
    if "blocking_warnings" in raw:
        raw["blocking_warnings"] = tuple(str(w) for w in raw["blocking_warnings"] or ())
    return SlicerConfig(**raw)


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
            bed_check=_bed_check(p.get("bed_check")),
            auto_start_hours=p.get("auto_start_hours") or None,
            finish_window=p.get("finish_window") or None,
            maintenance=parse_maintenance(p.get("maintenance")),
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
            publish_entities=bool(h.get("publish_entities", True)),
            mqtt=_mqtt(h.get("mqtt")),
        )
    ob = raw.get("obico") or {}
    st = raw.get("storage") or {}
    sv = raw.get("server") or {}
    pol = raw.get("policy") or {}
    host = sv.get("host", "0.0.0.0")
    port = int(sv.get("port", 8720))
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
        slicer=_slicer(raw.get("slicer") or {}),
        spoolman_url=((raw.get("spoolman") or {}).get("url") or "").rstrip("/") or None,
        api_token=os.environ.get(sv.get("token_env", "PRINT_WARDEN_TOKEN")) or None,
        approval_ttl_minutes=float(pol.get("approval_ttl_minutes", 15)),
        quiet_hours=(raw.get("alerts") or {}).get("quiet_hours") or None,
        alert_cooldown_s=float((raw.get("alerts") or {}).get("cooldown_s", 300)),
        quiet_entity=(raw.get("alerts") or {}).get("quiet_entity") or None,
        quiet_states=tuple(
            str(s) for s in (raw.get("alerts") or {}).get("quiet_states") or ("on",)
        ),
        upkeep=Upkeep(
            reorder_below_g=float(
                (raw.get("upkeep") or {}).get("reorder_below_g", 200)
            ),
            dry_after_days={
                str(k).upper(): float(v)
                for k, v in (
                    (raw.get("upkeep") or {}).get("dry_after_days") or {}
                ).items()
            },
        ),
    )
