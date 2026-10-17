"""`print-warden check`: is this config ready to run, before it is deployed.

Read-only: every probe is a GET (Moonraker, cameras, Home Assistant, Spoolman), a profile resolution, a file test or
an MQTT connect-and-disconnect. Nothing is printed, moved, heated or published. Each result is ok, warn (works, but
something an operator probably wants is missing) or fail (the warden will not do its job); the exit code is 1 when
anything fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
import httpx
import yaml

from . import config
from .slicer import flatten, search_dirs

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Result:
    status: str
    area: str
    detail: str


def _why(e: BaseException) -> str:
    return str(e) or type(e).__name__


MqttProbe = Callable[[config.Mqtt], Awaitable[None]]


async def _mqtt_connect(cfg: config.Mqtt) -> None:
    async with aiomqtt.Client(
        cfg.host,
        cfg.port,
        username=cfg.username,
        password=cfg.password,
        identifier="print-warden-check",
        timeout=10,
    ):
        pass


def _slicer(cfg: config.Config) -> list[Result]:
    s = cfg.slicer
    out: list[Result] = []
    orca = Path(s.orca)
    out.append(
        Result(OK, "slicer", f"OrcaSlicer at {orca}")
        if orca.is_file() and os.access(orca, os.X_OK)
        else Result(FAIL, "slicer", f"OrcaSlicer not executable at {orca}")
    )
    if not Path(s.vendor_dir).is_dir():
        out.append(
            Result(FAIL, "slicer", f"vendor profiles not found at {s.vendor_dir}")
        )
    if s.house_rules:
        try:
            json.loads(Path(s.house_rules).read_text())
            out.append(Result(OK, "slicer", f"house rules {s.house_rules}"))
        except (OSError, ValueError) as e:
            out.append(
                Result(FAIL, "slicer", f"house rules {s.house_rules}: {_why(e)}")
            )
    else:
        out.append(
            Result(
                WARN,
                "slicer",
                "no house rules: Orca's defaults apply (Cool Plate bed temperatures)",
            )
        )
    out.append(
        Result(OK, "slicer", f"OpenSCAD: {s.openscad}")
        if shutil.which(s.openscad)
        else Result(
            WARN, "slicer", f"OpenSCAD not found ({s.openscad}); .scad models will fail"
        )
    )
    if not shutil.which("xvfb-run"):
        out.append(Result(FAIL, "slicer", "xvfb-run not found; Orca's CLI needs it"))
    return out


def _profiles(cfg: config.Config, p: config.Printer) -> list[Result]:
    area = f"{p.name} profiles"
    if not p.machine:
        return [Result(WARN, area, "no machine profile: this printer cannot slice")]
    vendor = search_dirs(cfg.slicer)
    wanted = [("machine", p.machine)]
    wanted += [("process", v) for v in p.processes.values()]
    wanted += [("filament", v) for v in p.filaments.values()]
    bad = []
    for kind, name in wanted:
        try:
            flatten(vendor, kind, name)
        except (OSError, KeyError, ValueError) as e:
            bad.append(f"{kind} {name!r}: {_why(e)}")
    if bad:
        return [Result(FAIL, area, "; ".join(bad))]
    return [
        Result(
            OK,
            area,
            f"machine + {len(p.processes)} qualities + {len(p.filaments)} materials resolve",
        )
    ]


async def _printer(c: httpx.AsyncClient, p: config.Printer) -> list[Result]:
    out: list[Result] = []
    try:
        r = await c.get(f"{p.moonraker.rstrip('/')}/server/info")
        r.raise_for_status()
        klippy = r.json().get("result", {}).get("klippy_state")
        out.append(
            Result(
                OK if klippy == "ready" else WARN,
                f"{p.name} moonraker",
                f"reachable, Klipper {klippy}",
            )
        )
    except (httpx.HTTPError, ValueError) as e:
        out.append(Result(FAIL, f"{p.name} moonraker", f"{p.moonraker}: {_why(e)}"))
    if p.power_device:
        try:
            r = await c.get(f"{p.moonraker.rstrip('/')}/machine/device_power/devices")
            r.raise_for_status()
            names = {
                d.get("device") for d in r.json().get("result", {}).get("devices", [])
            }
            out.append(
                Result(
                    OK, f"{p.name} power", f"Moonraker power device {p.power_device}"
                )
                if p.power_device in names
                else Result(
                    FAIL,
                    f"{p.name} power",
                    f"Moonraker has no power device {p.power_device!r} ({sorted(names)})",
                )
            )
        except (httpx.HTTPError, ValueError) as e:
            out.append(Result(FAIL, f"{p.name} power", f"{p.moonraker}: {_why(e)}"))
    if not p.cameras:
        out.append(
            Result(
                WARN, f"{p.name} cameras", "none: no bed check, snapshots or timelapse"
            )
        )
    for cam, url in p.cameras.items():
        try:
            r = await c.get(url)
            r.raise_for_status()
            kind = r.headers.get("content-type", "")
            out.append(
                Result(OK, f"{p.name} camera {cam}", f"{len(r.content)} bytes {kind}")
                if kind.startswith("image/")
                else Result(
                    FAIL,
                    f"{p.name} camera {cam}",
                    f"{url} returned {kind or 'no content type'}, not an image",
                )
            )
        except httpx.HTTPError as e:
            out.append(Result(FAIL, f"{p.name} camera {cam}", f"{url}: {_why(e)}"))
    if p.bed_check and p.bed_check.camera not in p.cameras:
        out.append(
            Result(
                FAIL,
                f"{p.name} bed check",
                f"camera {p.bed_check.camera!r} is not one of {sorted(p.cameras)}",
            )
        )
    return out


async def _home_assistant(c: httpx.AsyncClient, cfg: config.Config) -> list[Result]:
    ha = cfg.ha
    if not ha:
        return [
            Result(
                WARN,
                "home assistant",
                "not configured: no alerts, prompts or start taps",
            )
        ]
    if not ha.token:
        return [Result(FAIL, "home assistant", "token env var is empty")]
    h = {"Authorization": f"Bearer {ha.token}"}
    try:
        r = await c.get(f"{ha.url}/api/", headers=h)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return [Result(FAIL, "home assistant", f"{ha.url}/api/: {_why(e)}")]
    out = [Result(OK, "home assistant", f"{ha.url} accepts the token")]
    if not ha.action_secret:
        out.append(
            Result(
                WARN, "home assistant", "HA_ACTION_SECRET unset: phone taps are refused"
            )
        )
    entities: list[tuple[str, str]] = []
    if ha.notify_service:
        entities.append(("notify service", ha.notify_service))
    if cfg.quiet_entity:
        entities.append(("quiet entity", cfg.quiet_entity))
    for p in cfg.printers.values():
        if p.light:
            entities.append((f"{p.name} light", p.light))
    services: set[str] | None = None
    for label, entity in entities:
        if label == "notify service":
            if services is None:
                r = await c.get(f"{ha.url}/api/services", headers=h)
                services = (
                    {
                        f"{d['domain']}.{s}"
                        for d in r.json()
                        for s in d.get("services", {})
                    }
                    if r.status_code == 200
                    else set()
                )
            found = entity in services
        else:
            found = (
                await c.get(f"{ha.url}/api/states/{entity}", headers=h)
            ).status_code == 200
        out.append(
            Result(OK, "home assistant", f"{label} {entity}")
            if found
            else Result(FAIL, "home assistant", f"{label} {entity} does not exist")
        )
    return out


async def _spoolman(c: httpx.AsyncClient, cfg: config.Config) -> list[Result]:
    if not cfg.spoolman_url:
        return [
            Result(
                WARN,
                "spoolman",
                "not configured: no spool tracking or remaining-weight blocker",
            )
        ]
    try:
        r = await c.get(f"{cfg.spoolman_url}/api/v1/health")
        r.raise_for_status()
    except httpx.HTTPError as e:
        return [Result(FAIL, "spoolman", f"{cfg.spoolman_url}: {_why(e)}")]
    return [Result(OK, "spoolman", cfg.spoolman_url)]


async def _mqtt(cfg: config.Config, probe: MqttProbe) -> list[Result]:
    if not (cfg.ha and cfg.ha.mqtt):
        return []
    m = cfg.ha.mqtt
    try:
        await probe(m)
    except (aiomqtt.MqttError, OSError) as e:
        return [Result(FAIL, "mqtt", f"{m.host}:{m.port}: {_why(e)}")]
    return [Result(OK, "mqtt", f"{m.host}:{m.port} accepts the connection")]


def _local(cfg: config.Config) -> list[Result]:
    out = []
    try:
        cfg.storage_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.storage_dir / ".check"
        probe.write_text("ok")
        probe.unlink()
        out.append(Result(OK, "storage", f"{cfg.storage_dir} is writable"))
    except OSError as e:
        out.append(Result(FAIL, "storage", f"{cfg.storage_dir}: {_why(e)}"))
    if not cfg.api_token:
        out.append(
            Result(
                WARN, "server", "PRINT_WARDEN_TOKEN unset: /mcp and /upload are open"
            )
        )
    return out


async def run(
    cfg: config.Config,
    client: httpx.AsyncClient | None = None,
    mqtt_probe: MqttProbe = _mqtt_connect,
) -> list[Result]:
    own = client is None
    c = client or httpx.AsyncClient(timeout=10, follow_redirects=True)
    # Home Assistant gets its own client so `verify_tls: false` reaches the check, as
    # it does the warden: HA is usually reached by IP with a certificate for its name.
    ha_c = c
    if own and cfg.ha and not cfg.ha.verify_tls:
        ha_c = httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False)
    try:
        results = _local(cfg) + _slicer(cfg)
        for p in cfg.printers.values():
            results += _profiles(cfg, p)
            results += await _printer(c, p)
        results += await _home_assistant(ha_c, cfg)
        results += await _spoolman(c, cfg)
        results += await _mqtt(cfg, mqtt_probe)
        return results
    finally:
        if own:
            await c.aclose()
            if ha_c is not c:
                await ha_c.aclose()


def main(argv: list[str]) -> int:
    path = argv[0] if argv else os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    try:
        cfg = config.load(path)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as e:
        print(f"fail  config        {path}: {_why(e)}")
        return 1
    print(f"ok    config        {path}: {len(cfg.printers)} printer(s)")
    results = asyncio.run(run(cfg))
    for r in results:
        print(f"{r.status:<5} {r.area:<13} {r.detail}")
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    print(f"\n{fails} failed, {warns} warning(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
