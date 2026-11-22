"""`print-warden add-printer`: a new printer from its Moonraker, ready for `check` and a short sign-off.

    print-warden add-printer NAME --moonraker http://printer.local:7125 [--location L] [--camera bed=URL ...]
                             [--base PRESET] [--config printers.yaml] [--write [PATH]]

It reads the printer from Moonraker (read-only):
- Klipper's state and parsed config, from which `profiles klipper` writes the machine preset (bed, height, nozzle,
  speeds, `PRINT_START`/`PRINT_END` with their parameters) into the warden's profiles;
- the webcams Moonraker knows, as cameras (unless `--camera` names them);
- a Moonraker power device, as `power_device`.

It then builds the printer's entry: location, Moonraker, cameras, power device and the `slicer:` block (that preset,
with the processes and filaments OrcaSlicer pairs with it). It prints the entry and, with `--write`, inserts it under
`printers:` in a copy of the config (comments kept), checks that the result loads, and writes it to PATH (default:
the config itself, which is read-only in the container, so write under `data/` there). Last it runs `print-warden
check` for the new printer and names the sign-off rows to run once the warden has restarted with it. It never
moves, heats or prints.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import check, config, profiles

SIGNOFF_ROWS = "0,1,3,4,11,12,39"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


async def discover(moonraker: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """What Moonraker says about the printer: Klipper's state, its config, webcams and power devices."""
    base = moonraker.rstrip("/")
    info = (await client.get(f"{base}/server/info")).json().get("result") or {}
    settings = await profiles.klipper_settings(base, client)
    webcams: list[dict[str, Any]] = []
    r = await client.get(f"{base}/server/webcams/list")
    if r.status_code == 200:
        webcams = list((r.json().get("result") or {}).get("webcams") or [])
    devices: list[str] = []
    r = await client.get(f"{base}/machine/device_power/devices")
    if r.status_code == 200:
        devices = [
            str(d.get("device"))
            for d in (r.json().get("result") or {}).get("devices") or []
        ]
    return {
        "klippy": info.get("klippy_state"),
        "settings": settings,
        "webcams": webcams,
        "devices": devices,
    }


def cameras_from(webcams: list[dict[str, Any]], moonraker: str) -> dict[str, str]:
    """Moonraker's webcams as `cameras:` (name -> snapshot URL); relative URLs are taken from Moonraker's host."""
    host = httpx.URL(moonraker)
    out: dict[str, str] = {}
    for cam in webcams:
        url = str(cam.get("snapshot_url") or "")
        if not url:
            continue
        if url.startswith("/"):
            url = str(
                host.copy_with(
                    path=url.split("?", 1)[0],
                    query=url.partition("?")[2].encode() or None,
                    port=None,
                )
            )
        name = (
            re.sub(r"[^a-z0-9]+", "_", str(cam.get("name") or "cam").lower()).strip("_")
            or "cam"
        )
        out.setdefault(
            "bed"
            if not out and name in ("cam", "webcam", "camera", "default")
            else name,
            url,
        )
    return out


def entry(
    name: str,
    moonraker: str,
    found: dict[str, Any],
    slicer_block: dict[str, Any],
    location: str | None = None,
    cameras: dict[str, str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if location:
        out["location"] = location
    out["moonraker"] = moonraker.rstrip("/")
    out["cameras"] = (
        cameras if cameras is not None else cameras_from(found["webcams"], moonraker)
    )
    if len(found["devices"]) == 1:
        out["power_device"] = found["devices"][0]
    out["slicer"] = slicer_block
    return out


def insert(text: str, name: str, printer: dict[str, Any]) -> str:
    """The config text with `name` added under `printers:`, every other line (comments too) as it was."""
    raw = yaml.safe_load(text) or {}
    if name in (raw.get("printers") or {}):
        raise ValueError(f"{name} is already in the config")
    block = yaml.safe_dump({name: printer}, sort_keys=False, allow_unicode=True)
    indented = "".join(
        f"  {line}\n" if line.strip() else "\n" for line in block.splitlines()
    )
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^printers:\s*(#.*)?$", line):
            return "".join(lines[: i + 1]) + indented + "".join(lines[i + 1 :])
        if re.match(r"^printers:\s*\{\s*\}\s*(#.*)?$", line):
            return (
                "".join(lines[:i]) + "printers:\n" + indented + "".join(lines[i + 1 :])
            )
    return (
        text.rstrip("\n") + ("\n\n" if text.strip() else "") + "printers:\n" + indented
    )


def _unknown(text: str) -> set[str]:
    return set(config.unknown_keys(yaml.safe_load(text) or {}))


def _check_text(text: str, before: str) -> config.Config:
    """The new config, loaded; a key it does not know that the old config did not have is an error."""
    new = sorted(_unknown(text) - _unknown(before))
    if new:
        raise ValueError("the new config has unknown keys: " + ", ".join(new))
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
    try:
        return config.load(f.name)
    finally:
        os.unlink(f.name)


async def _check(
    cfg: config.Config, name: str, client: httpx.AsyncClient
) -> list[check.Result]:
    """`print-warden check`'s rows for this printer only: its profiles and its Moonraker."""
    p = cfg.printers[name]
    return check._profiles(cfg, p) + await check._printer(client, p)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="print-warden add-printer", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "name", help="the printer's name in the warden (letters, digits, - and _)"
    )
    ap.add_argument("--moonraker", required=True, help="e.g. http://printer.local:7125")
    ap.add_argument("--location", help="where it is (for submit_job by location)")
    ap.add_argument(
        "--camera",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="a camera snapshot URL",
    )
    ap.add_argument(
        "--base", help="the OrcaSlicer machine preset to inherit (default MyKlipper)"
    )
    ap.add_argument(
        "--config", default=os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    )
    ap.add_argument(
        "--write",
        nargs="?",
        const="",
        metavar="PATH",
        help="write the config with the printer added",
    )
    return ap


def main(argv: list[str], client: httpx.AsyncClient | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _NAME.match(args.name):
        print(f"fail: {args.name!r}: use letters, digits, - and _")
        return 2
    cams: dict[str, str] | None = None
    if args.camera:
        cams = {}
        for c in args.camera:
            k, _, v = c.partition("=")
            if not (k and v):
                print(f"fail: --camera {c!r}: NAME=URL")
                return 2
            cams[k] = v
    cfg_path = Path(args.config)
    text = cfg_path.read_text() if cfg_path.exists() else ""
    dirs, user, _ = profiles._setup(args.config)

    async def go() -> tuple[dict[str, Any], str, list[check.Result]]:
        own = client is None
        c = client or httpx.AsyncClient(timeout=15, follow_redirects=True)
        try:
            found = await discover(args.moonraker, c)
            if found["klippy"] != "ready":
                print(
                    f"note: Klipper is {found['klippy']!r}; the profile comes from its last loaded config"
                )
            preset = profiles.from_klipper(
                found["settings"], f"{args.name} (Klipper)", dirs, args.base
            )
            dst = profiles.write_preset(user, preset)
            print(f"wrote the machine preset {dst} (inherits {preset['inherits']})")
            block = profiles.block([user, *dirs], preset["name"])
            printer = entry(
                args.name, args.moonraker, found, block, args.location, cams
            )
            new_text = insert(text, args.name, printer)
            return (
                printer,
                new_text,
                await _check(_check_text(new_text, text), args.name, c),
            )
        finally:
            if own:
                await c.aclose()

    try:
        printer, new_text, mine = asyncio.run(go())
    except (OSError, ValueError, KeyError, httpx.HTTPError, yaml.YAMLError) as e:
        print(f"fail: {e}")
        return 1
    slicer_block = printer["slicer"]
    print(
        "\n"
        + yaml.safe_dump({args.name: printer}, sort_keys=False, allow_unicode=True),
        end="",
    )
    if not slicer_block.get("processes") or not slicer_block.get("filaments"):
        print(
            "note: OrcaSlicer pairs no processes or filaments with this preset; fill them in from `profiles block`"
        )
    if args.write is not None:
        out = Path(args.write or args.config)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(new_text)
        except OSError as e:
            print(
                f"fail: cannot write {out}: {e} (in the container, --write data/printers.yaml and copy it)"
            )
            return 1
        print(f"wrote {out}")
    for r in mine:
        print(f"{r.status:<5} {r.area:<13} {r.detail}")
    fails = sum(r.status == check.FAIL for r in mine)
    print(
        f"\n{fails} failed for {args.name}. Next: restart the warden with the new config, then\n"
        f"  print-warden validate session --printer {args.name} --rows {SIGNOFF_ROWS} --by <initials>"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
