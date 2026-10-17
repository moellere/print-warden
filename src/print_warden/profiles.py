"""Printer profiles for any printer: OrcaSlicer's bundled vendors, a user's own presets, or one made from Klipper.

- Bundled: every vendor directory next to `slicer.vendor_dir` is searched (`slicer.search_dirs`), so any Orca
  printer can be named in `printers.yaml`. `block` writes that printer's slicer block: the processes Orca lists as
  compatible with it (keyed by quality), and per material the warden checks, the vendor's generic filament for it
  (else Orca's universal one).
- Imported: Orca / Bambu Studio presets exported as `.json`, or bundles (`.orca_printer`, `.orca_filament`,
  `.bbscfg`, `.bbsflmt`, `.zip`), are written to the warden's profile directory (`slicer.user_dir`) by name. Their
  `inherits` must resolve against the bundled vendors or the same import; one that does not is refused. A process's
  `post_process` (a command Orca runs on this host) is dropped.
- From Klipper: Moonraker's parsed `printer.cfg` gives the bed (stepper limits), height, nozzle, kinematics, speed
  and acceleration limits and the start/end macros; the result inherits a generic Orca Klipper printer of the same
  nozzle, so its processes and Orca's universal filaments apply.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import config
from .checks import MATERIAL_TEMPS
from .config import SlicerConfig
from .slicer import KINDS, LIBRARY, _index, find, flatten, search_dirs

BUNDLES = (".orca_printer", ".orca_filament", ".bbscfg", ".bbsflmt", ".zip")
KLIPPER_BASE = "MyKlipper {nozzle} nozzle"
_QUALITY = re.compile(r"^\s*([\d.]+)\s*mm\s+(.+?)\s*(?:@.*)?$", re.IGNORECASE)
_SAFE = re.compile(r"[^\w .@()+,-]")


def _load(path: str) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text()))


def vendors(dirs: list[Path]) -> list[str]:
    """Vendor directories with at least one printer to pick."""
    return sorted(
        d.name for d in dirs if d.name != LIBRARY and printers(dirs, d.name, quick=True)
    )


def printers(dirs: list[Path], vendor: str, quick: bool = False) -> list[str]:
    """The printers (instantiated machine presets) of one vendor."""
    out = []
    for d in dirs:
        if d.name != vendor:
            continue
        for name, path in sorted(_index(d, "machine").items()):
            if str(_load(path).get("instantiation", "true")).lower() == "true":
                out.append(name)
                if quick:
                    break
    return out


def _aliases(dirs: list[Path], machine: str) -> tuple[set[str], set[Path]]:
    """The names compatibility lists may use for the machine: its own and, for a user's copy, its parents up to the
    first bundled preset (a bundled printer can inherit a sibling, e.g. another nozzle, which is not an alias). Also
    the directories of the whole chain: universal presets are taken only from those and Orca's library."""
    names = {machine}
    preset, home = find(dirs, "machine", machine)
    homes = {home, *(d for d in dirs if d.name == LIBRARY)}
    bundled = str(preset.get("from", "")).lower() != "user"
    while parent := preset.get("inherits"):
        preset, home = find(dirs, "machine", parent, home)
        homes.add(home)
        if not bundled:
            names.add(parent)
            bundled = str(preset.get("from", "")).lower() != "user"
    return names, homes


def _compatible(preset: dict[str, Any], aliases: set[str]) -> int | None:
    """0 when the preset names the printer, 1 when it fits any printer, None when it is for others."""
    listed = preset.get("compatible_printers")
    if listed:
        return 0 if aliases & set(listed) else None
    return None if preset.get("compatible_printers_condition") else 1


def _candidates(
    dirs: list[Path], kind: str, machine: str
) -> list[tuple[int, int, str]]:
    """(fit, source, name) of each preset that fits the printer; source ranks the user's own presets, then the
    vendor's, then Orca's library. A preset without its own compatibility list inherits its parent's."""
    aliases, homes = _aliases(dirs, machine)
    out = []
    for d in dirs:
        for name, path in _index(d, kind).items():
            p = _load(path)
            if str(p.get("instantiation", "true")).lower() != "true":
                continue
            if "compatible_printers" not in p and p.get("inherits"):
                p = flatten(dirs, kind, name)
            rank = _compatible(p, aliases)
            if rank == 0 or (rank == 1 and d in homes):
                mine = str(p.get("from", "")).lower() == "user"
                out.append((rank, 0 if mine else 2 if d.name == LIBRARY else 1, name))
    return out


def _quality(name: str, height: float | None) -> str:
    m = _QUALITY.match(name)
    word = m[2] if m else name.split("@")[0]
    return re.sub(r"\W+", "_", word.strip().lower()).strip("_") or f"h{height}"


def processes(dirs: list[Path], machine: str) -> dict[str, str]:
    """Quality -> process preset for this printer. Names repeat ("Standard" at every height on Prusa), so the one
    nearest 0.2 mm keeps the bare key and the rest get the height; `standard` always exists when any process does."""
    found: list[tuple[str, float, str]] = []
    for rank, _, name in sorted(set(_candidates(dirs, "process", machine))):
        if rank:
            continue
        h = float(flatten(dirs, "process", name).get("layer_height") or 0) or None
        found.append((_quality(name, h), h or 0.0, name))
    out: dict[str, str] = {}
    for key, h, name in sorted(found, key=lambda f: (f[0], abs(f[1] - 0.2), f[2])):
        out[key if key not in out else f"{key}_{h:.2f}"] = name
    if out and "standard" not in out:
        out["standard"] = min(found, key=lambda f: (abs(f[1] - 0.2), f[2]))[2]
    return dict(sorted(out.items()))


def filaments(dirs: list[Path], machine: str) -> dict[str, str]:
    """Per material the warden checks: the user's own filament for it, else the vendor's generic one, else Orca's
    universal one."""
    best: dict[str, tuple[tuple[int, int, int, int, str], str]] = {}
    for rank, library, name in _candidates(dirs, "filament", machine):
        f = flatten(dirs, "filament", name)
        kind = f.get("filament_type")
        mat = str(kind[0] if isinstance(kind, list) and kind else kind or "").upper()
        if mat not in MATERIAL_TEMPS:
            continue
        key = (rank, library, 0 if "generic" in name.lower() else 1, len(name), name)
        if mat not in best or key < best[mat][0]:
            best[mat] = (key, name)
    return {m: best[m][1] for m in MATERIAL_TEMPS if m in best}


def block(dirs: list[Path], machine: str) -> dict[str, Any]:
    """The `slicer:` block of a printer in printers.yaml."""
    flatten(dirs, "machine", machine)
    return {
        "machine": machine,
        "processes": processes(dirs, machine),
        "filaments": filaments(dirs, machine),
    }


def _presets_in(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """(where, preset) for a preset file or every preset in a bundle; members are read, never extracted."""
    if path.suffix.lower() not in BUNDLES:
        return [(path.name, json.loads(path.read_text()))]
    out = []
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".json"):
                continue
            if info.file_size > 4 << 20:
                raise ValueError(
                    f"{path.name}: {info.filename} is too large for a preset"
                )
            data = json.loads(z.read(info))
            if isinstance(data, dict) and "bundle_type" not in data:
                out.append((f"{path.name}:{info.filename}", data))
    return out


def _kind(where: str, preset: dict[str, Any]) -> str:
    kind = str(preset.get("type") or "").lower()
    if kind in KINDS:
        return kind
    parts = where.split(":", 1)[-1].replace("\\", "/").lower().split("/")
    folder = parts[-2] if len(parts) > 1 else ""
    if folder in ("printer", "machine"):
        return "machine"
    if folder in KINDS:
        return folder
    raise ValueError(f"{where}: not a printer, process or filament preset")


def import_presets(
    paths: list[Path], user_dir: Path, dirs: list[Path]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Write each preset to `user_dir/<kind>/<name>.json`. Returns ((kind, name) imported, problems); a preset whose
    `inherits` does not resolve is removed again and named in the problems."""
    written: list[tuple[str, str, Path]] = []
    problems: list[str] = []
    for path in paths:
        for where, preset in _presets_in(path):
            name = str(preset.get("name") or "").strip()
            if not name:
                problems.append(f"{where}: no name")
                continue
            kind = _kind(where, preset)
            preset = {**preset, "name": name, "type": kind, "from": "User"}
            if preset.get("post_process"):
                preset["post_process"] = []
                problems.append(
                    f"{kind} {name!r}: post_process dropped (Orca would run it on this host)"
                )
            dst = user_dir / kind / f"{_SAFE.sub('_', name)}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(json.dumps(preset, indent=2))
            written.append((kind, name, dst))
    search = [user_dir, *(d for d in dirs if d != user_dir)]
    done = []
    for kind, name, dst in written:
        try:
            flatten(search, kind, name)
            done.append((kind, name))
        except (FileNotFoundError, ValueError) as e:
            dst.unlink(missing_ok=True)
            problems.append(
                f"{kind} {name!r} not imported: {e} (export it with its parent, or as a full preset)"
            )
    return done, problems


def _num(v: Any, default: float | None = None) -> float | None:
    try:
        return float(str(v).split(",")[0])
    except (TypeError, ValueError):
        return default


def _g(v: float) -> str:
    return f"{v:g}"


def _macro(settings: dict[str, Any], names: tuple[str, ...]) -> tuple[str, str] | None:
    for n in names:
        if sec := settings.get(f"gcode_macro {n}"):
            return n.upper(), str(sec.get("gcode") or "")
    return None


def _param(body: str, *prefixes: str) -> str | None:
    for p in re.findall(r"params\.(\w+)", body, re.IGNORECASE):
        if p.upper().startswith(prefixes):
            return p.upper()
    return None


def _start_gcode(settings: dict[str, Any], x0: float, y0: float) -> str:
    bed, hot = (
        "[bed_temperature_initial_layer_single]",
        "[nozzle_temperature_initial_layer]",
    )
    if m := _macro(settings, ("print_start", "start_print")):
        name, body = m
        b, e = _param(body, "BED"), _param(body, "EXTRUDER", "HOTEND", "NOZZLE")
        if b and e:
            return f"{name} {b}={bed} {e}={hot}\n"
        return f"M190 S{bed}\nM109 S{hot}\n{name}\n"
    return (
        f"M190 S{bed}\nM109 S{hot}\nG28\nG90\nM83\nG92 E0\n"
        f"G1 X{_g(x0 + 5)} Y{_g(y0 + 5)} Z0.3 F6000\nG1 X{_g(x0 + 85)} E12 F1500\nG92 E0\n"
    )


def _end_gcode(settings: dict[str, Any]) -> str:
    if m := _macro(settings, ("print_end", "end_print")):
        return m[0] + "\n"
    return "M104 S0\nM140 S0\nG91\nG1 Z10 F600\nG90\nM106 S0\nM84\n"


STRUCTURE = {
    "corexy": "corexy",
    "cartesian": "i3",
    "delta": "delta",
    "hybrid_corexy": "corexy",
}


def from_klipper(
    settings: dict[str, Any], name: str, dirs: list[Path], base: str | None = None
) -> dict[str, Any]:
    """A machine preset from Klipper's parsed config (Moonraker's `configfile.settings`)."""
    sx, sy, sz = (settings.get(f"stepper_{a}") or {} for a in "xyz")
    ext = settings.get("extruder") or {}
    printer = settings.get("printer") or {}
    kin = str(printer.get("kinematics") or "").lower()
    if kin == "delta":
        raise ValueError(
            "delta printers need a round bed profile; start from a vendor delta preset"
        )
    x1, y1 = _num(sx.get("position_max")), _num(sy.get("position_max"))
    z1 = _num(sz.get("position_max"))
    if not (x1 and y1 and z1):
        raise ValueError(
            "printer.cfg has no stepper_x/y/z position_max to size the bed from"
        )
    x0 = max(0.0, _num(sx.get("position_min"), 0.0) or 0.0)
    y0 = max(0.0, _num(sy.get("position_min"), 0.0) or 0.0)
    nozzle = _num(ext.get("nozzle_diameter"), 0.4) or 0.4
    if base is None:
        base = KLIPPER_BASE.format(nozzle=_g(nozzle))
        try:
            find(dirs, "machine", base)
        except FileNotFoundError:
            base = KLIPPER_BASE.format(nozzle="0.4")
    find(dirs, "machine", base)
    preset: dict[str, Any] = {
        "type": "machine",
        "name": name,
        "inherits": base,
        "from": "User",
        "printable_area": [f"{_g(x0)}x{_g(y0)}", f"{_g(x1)}x{_g(y0)}", f"{_g(x1)}x{_g(y1)}", f"{_g(x0)}x{_g(y1)}"],
        "printable_height": _g(z1),
        "nozzle_diameter": [_g(nozzle)],
        "printer_variant": _g(nozzle),
        "gcode_flavor": "klipper",
        "machine_start_gcode": _start_gcode(settings, x0, y0),
        "machine_end_gcode": _end_gcode(settings),
    }  # fmt: skip
    if s := STRUCTURE.get(kin):
        preset["printer_structure"] = s
    if v := _num(printer.get("max_velocity")):
        for axis in "xy":
            preset[f"machine_max_speed_{axis}"] = [_g(v), _g(v)]
    if acc := _num(printer.get("max_accel")):
        for k in ("x", "y", "extruding", "travel"):
            preset[f"machine_max_acceleration_{k}"] = [_g(acc), _g(acc)]
    if fd := _num(ext.get("filament_diameter")):
        preset["filament_diameter"] = [_g(fd)]
    return preset


async def klipper_settings(moonraker: str, client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(
        f"{moonraker.rstrip('/')}/printer/objects/query?configfile=settings"
    )
    r.raise_for_status()
    return dict(r.json()["result"]["status"]["configfile"]["settings"])


def write_preset(user_dir: Path, preset: dict[str, Any]) -> Path:
    dst = user_dir / preset["type"] / f"{_SAFE.sub('_', preset['name'])}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(preset, indent=2))
    return dst


def _yaml_block(b: dict[str, Any]) -> str:
    return "# under the printer in printers.yaml:\n" + yaml.safe_dump(
        {"slicer": b}, sort_keys=False, allow_unicode=True
    )


def _setup(path: str) -> tuple[list[Path], Path]:
    if Path(path).exists():
        s = config.load(path).slicer
    else:
        s = SlicerConfig(user_dir="data/profiles")
        print(f"# no {path}: using {s.vendor_dir} and {s.user_dir}", file=sys.stderr)
    user = Path(s.user_dir or "data/profiles")
    return search_dirs(s), user


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="print-warden profiles",
        description="Find, import or generate OrcaSlicer printer profiles.",
    )
    ap.add_argument(
        "--config", default=os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("vendors", help="vendors in OrcaSlicer's bundled profiles")
    sub.add_parser("printers", help="a vendor's printers").add_argument("vendor")
    sub.add_parser("block", help="the printers.yaml slicer block").add_argument(
        "machine"
    )
    imp = sub.add_parser("import", help="Orca / Bambu Studio presets or bundles")
    imp.add_argument("files", nargs="+", type=Path)
    kl = sub.add_parser("klipper", help="a printer profile from Klipper's config")
    kl.add_argument("name", help="name for the new printer preset")
    kl.add_argument("moonraker", help="Moonraker URL, e.g. http://printer.local:7125")
    kl.add_argument("--base", help="machine preset to inherit (default: MyKlipper)")
    a = ap.parse_args(argv)
    dirs, user = _setup(a.config)
    try:
        if a.cmd == "vendors":
            print("\n".join(vendors(dirs)))
        elif a.cmd == "printers":
            names = printers(dirs, a.vendor)
            if not names:
                print(f"no printers for vendor {a.vendor!r}", file=sys.stderr)
                return 1
            print("\n".join(names))
        elif a.cmd == "block":
            print(_yaml_block(block(dirs, a.machine)), end="")
        elif a.cmd == "import":
            done, problems = import_presets(a.files, user, dirs)
            for kind, name in done:
                print(f"imported {kind:<8} {name}")
            for p in problems:
                print(f"note     {p}", file=sys.stderr)
            search = [user, *(d for d in dirs if d != user)]
            for kind, name in done:
                if kind == "machine":
                    print(_yaml_block(block(search, name)), end="")
            return 0 if done and not any("not imported" in p for p in problems) else 1
        else:
            settings = asyncio.run(_settings(a.moonraker))
            preset = from_klipper(settings, a.name, dirs, a.base)
            dst = write_preset(user, preset)
            print(f"wrote {dst} (inherits {preset['inherits']})")
            print(_yaml_block(block([user, *dirs], a.name)), end="")
    except (FileNotFoundError, ValueError, KeyError, httpx.HTTPError) as e:
        print(f"fail: {e}", file=sys.stderr)
        return 1
    return 0


async def _settings(moonraker: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        return await klipper_settings(moonraker, c)
