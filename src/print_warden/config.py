"""Configuration loading. YAML in, frozen dataclasses out; env vars hold secrets."""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .digest import day_index
from .presence import Presence
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
    profiles_root: str | None = None
    user_dir: str | None = None
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
    purge_zone: tuple[float, float, float, float] | None = None
    power_entity: str | None = None
    spool_scale_entity: str | None = None
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
class CheapPower:
    entity: str
    max_price: float
    min_hours: float = 3.0
    min_kwh: float | None = None


@dataclass(frozen=True)
class Energy:
    price_per_kwh: float | None = None
    price_entity: str | None = None


@dataclass(frozen=True)
class DryBox:
    name: str
    entity: str
    max_pct: float = 20.0


@dataclass(frozen=True)
class Upkeep:
    reorder_below_g: float = 200.0
    dry_after_days: dict[str, float] = field(default_factory=dict)
    weigh_every_days: float | None = None
    todo_entity: str | None = None
    dry_boxes: tuple[DryBox, ...] = ()


@dataclass(frozen=True)
class Digest:
    day: int = 6
    """Monday is 0."""
    at: str = "18:00"


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
    thingiverse_token: str | None = None
    approval_ttl_minutes: float = 15.0
    paused_cancel_minutes: float = 30.0
    quiet_hours: str | None = None
    alert_cooldown_s: float = 300.0
    quiet_entity: str | None = None
    quiet_states: tuple[str, ...] = ("on",)
    upkeep: Upkeep = Upkeep()
    cheap_power: CheapPower | None = None
    energy: Energy = Energy()
    presence: Presence | None = None
    digest: Digest | None = None


def _digest(raw: dict[str, Any] | None) -> Digest | None:
    if not raw:
        return None
    at = str(raw.get("at", "18:00"))
    hh, _, mm = at.partition(":")
    if not (
        hh.isdigit()
        and 0 <= int(hh) < 24
        and (not mm or (mm.isdigit() and 0 <= int(mm) < 60))
    ):
        raise ValueError(f"digest.at must be HH:MM, got {at!r}")
    return Digest(day=day_index(str(raw.get("day", "sunday"))), at=at)


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


def _purge_zone(name: str, raw: Any) -> tuple[float, float, float, float] | None:
    """[x0, y0, x1, y1] in bed mm: where the printer's start gcode may purge outside the printable area."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"{name}: purge_zone must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (float(v) for v in raw)
    if x0 > x1 or y0 > y1:
        raise ValueError(f"{name}: purge_zone needs x0 <= x1 and y0 <= y1")
    return x0, y0, x1, y1


def _presence(raw: dict[str, Any] | None) -> Presence | None:
    if not raw or not raw.get("entity"):
        return None
    cap = raw.get("away_offer_max_hours")
    return Presence(
        entity=str(raw["entity"]),
        home_states=tuple(str(s) for s in raw.get("home_states") or ("home",)),
        auto_start_when_away=bool(raw.get("auto_start_when_away", False)),
        away_offer_max_hours=float(cap) if cap is not None else None,
        poll_s=float(raw.get("poll_s", 60)),
    )


def _slicer(raw: dict[str, Any]) -> SlicerConfig:
    raw = dict(raw)
    if "blocking_warnings" in raw:
        raw["blocking_warnings"] = tuple(str(w) for w in raw["blocking_warnings"] or ())
    return SlicerConfig(**raw)


ANY = "*"
"""In SCHEMA: a map whose keys are the operator's own (printer names, cameras, materials); its values are unchecked."""
_POSE = {"x": None, "y": None, "z": None}
_PRINTER = {
    "location": None, "moonraker": None, "cameras": ANY, "rotate": ANY, "flip_v": ANY, "light": None,
    "rules": {**{f.name: None for f in fields(Rules)}, "temp_limits": ANY},
    "slicer": {"machine": None, "processes": ANY, "filaments": ANY},
    "auto_start": None, "auto_start_hours": None, "finish_window": None, "power_device": None,
    "track_spool_usage": None, "purge_zone": None, "power_entity": None, "spool_scale_entity": None,
    "bed_check": {"camera": None, "region": None, "threshold": None, "min_agreements": None, "pose": _POSE,
                  "rehome_y": None},
    "maintenance": {ANY: {"every_hours": None, "every_days": None}},
}  # fmt: skip
SCHEMA: dict[str, Any] = {
    "printers": {ANY: _PRINTER},
    "home_assistant": {
        "url": None, "webhook_id": None, "token_env": None, "verify_tls": None, "notify_service": None,
        "action_secret_env": None, "publish_entities": None,
        "mqtt": {"host": None, "port": None, "username": None, "password_env": None, "discovery_prefix": None},
    },
    "obico": {"url": None, "webhook_secret_env": None},
    "storage": {"dir": None, "keep_days": None},
    "server": {"host": None, "port": None, "public_url": None, "token_env": None},
    "slicer": {f.name: None for f in fields(SlicerConfig)},
    "spoolman": {"url": None},
    "intake": {"thingiverse_token_env": None},
    "policy": {
        "approval_ttl_minutes": None, "paused_cancel_minutes": None,
        "cheap_power": {"entity": None, "max_price": None, "min_hours": None, "min_kwh": None},
    },
    "alerts": {"quiet_hours": None, "cooldown_s": None, "quiet_entity": None, "quiet_states": None},
    "presence": {f.name: None for f in fields(Presence)},
    "energy": {"price_per_kwh": None, "price_entity": None},
    "digest": {"day": None, "at": None},
    "upkeep": {
        "reorder_below_g": None, "dry_after_days": ANY, "weigh_every_days": None, "todo_entity": None,
        "dry_boxes": [{"name": None, "entity": None, "max_pct": None}],
    },
}  # fmt: skip
"""Every key `load` reads: the public config surface (docs/stability.md). `check` warns on any other."""


def unknown_keys(raw: Any, schema: Any = None, at: str = "") -> list[str]:
    """Keys in a parsed config that the warden does not read, each with a close known key when there is one."""
    schema = SCHEMA if schema is None else schema
    if schema is None or schema == ANY:
        return []
    if isinstance(schema, list):
        out = []
        for i, item in enumerate(raw if isinstance(raw, list) else []):
            out += unknown_keys(item, schema[0], f"{at}[{i}]")
        return out
    if not isinstance(raw, dict):
        return []
    out = []
    for key, value in raw.items():
        where = f"{at}.{key}" if at else str(key)
        if ANY in schema:
            out += unknown_keys(value, schema[ANY], where)
        elif key in schema:
            out += unknown_keys(value, schema[key], where)
        else:
            close = difflib.get_close_matches(
                str(key), [k for k in schema if k != ANY], n=1
            )
            out.append(f"{where}" + (f" (did you mean {close[0]}?)" if close else ""))
    return out


def unknown_in(path: str | Path) -> list[str]:
    return unknown_keys(yaml.safe_load(Path(path).read_text()) or {})


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
            purge_zone=_purge_zone(name, p.get("purge_zone")),
            power_entity=p.get("power_entity") or None,
            spool_scale_entity=p.get("spool_scale_entity") or None,
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
    port = int(sv.get("port", 8710))
    storage = Path(st.get("dir", "./data"))
    return Config(
        printers=printers,
        ha=ha,
        obico_url=(ob.get("url") or "").rstrip("/") or None,
        obico_webhook_secret=os.environ.get(
            ob.get("webhook_secret_env", "OBICO_WEBHOOK_SECRET")
        ),
        storage_dir=storage,
        keep_days=int(st.get("keep_days", 14)),
        host=host,
        port=port,
        public_url=(sv.get("public_url") or f"http://{host}:{port}").rstrip("/"),
        slicer=_slicer(
            {"user_dir": str(storage / "profiles"), **(raw.get("slicer") or {})}
        ),
        spoolman_url=((raw.get("spoolman") or {}).get("url") or "").rstrip("/") or None,
        api_token=os.environ.get(sv.get("token_env", "PRINT_WARDEN_TOKEN")) or None,
        thingiverse_token=os.environ.get(
            (raw.get("intake") or {}).get("thingiverse_token_env", "THINGIVERSE_TOKEN")
        )
        or None,
        approval_ttl_minutes=float(pol.get("approval_ttl_minutes", 15)),
        paused_cancel_minutes=float(pol.get("paused_cancel_minutes", 30)),
        quiet_hours=(raw.get("alerts") or {}).get("quiet_hours") or None,
        alert_cooldown_s=float((raw.get("alerts") or {}).get("cooldown_s", 300)),
        quiet_entity=(raw.get("alerts") or {}).get("quiet_entity") or None,
        quiet_states=tuple(
            str(s) for s in (raw.get("alerts") or {}).get("quiet_states") or ("on",)
        ),
        cheap_power=CheapPower(
            entity=str(cp["entity"]),
            max_price=float(cp["max_price"]),
            min_hours=float(cp.get("min_hours", 3)),
            min_kwh=float(cp["min_kwh"]) if cp.get("min_kwh") is not None else None,
        )
        if (cp := pol.get("cheap_power"))
        else None,
        presence=_presence(raw.get("presence")),
        digest=_digest(raw.get("digest")),
        energy=Energy(
            price_per_kwh=float(en["price_per_kwh"])
            if (en := raw.get("energy") or {}).get("price_per_kwh") is not None
            else None,
            price_entity=en.get("price_entity")
            or ((pol.get("cheap_power") or {}).get("entity")),
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
            weigh_every_days=float(wd)
            if (wd := (raw.get("upkeep") or {}).get("weigh_every_days"))
            else None,
            todo_entity=(raw.get("upkeep") or {}).get("todo_entity") or None,
            dry_boxes=tuple(
                DryBox(str(b["name"]), str(b["entity"]), float(b.get("max_pct", 20)))
                for b in (raw.get("upkeep") or {}).get("dry_boxes") or []
            ),
        ),
    )
