"""Headless OrcaSlicer wrapper. Presets are flattened from the vendor profile directory (Orca's CLI
cannot resolve `inherits`), house rules are applied last, and the gcode is pulled out of the 3mf."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import checks
from . import park as parking
from .calibration import apply as calibrate_gcode
from .config import Pose, SlicerConfig
from .preview import render as render_preview
from .threemf import leaked as project_leaks


@dataclass
class SliceResult:
    gcode: Path
    threemf: Path
    preview: Path | None
    est_seconds: int | None
    filament_g: float | None
    filament_m: float | None
    layers: int | None
    max_z: float | None
    warnings: list[str] = field(default_factory=list)
    log_tail: str = ""
    blockers: list[str] = field(default_factory=list)
    cached: bool = False


LIBRARY = "OrcaFilamentLibrary"
KINDS = ("machine", "process", "filament")


@functools.lru_cache(maxsize=512)
def _index_at(directory: str, kind: str, stamp: int) -> dict[str, str]:
    """Preset name -> file under `directory/kind`, by each file's `name` (Orca's file names do not always match it),
    else its stem. `stamp` (the folder's mtime) drops the cache when a preset is added."""
    out: dict[str, str] = {}
    for f in sorted(Path(directory, kind).rglob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("type", kind) != kind:
            continue
        out.setdefault(str(d.get("name") or f.stem), str(f))
    return out


def _index(directory: Path, kind: str) -> dict[str, str]:
    folder = directory / kind
    if not folder.is_dir():
        return {}
    return _index_at(str(directory), kind, folder.stat().st_mtime_ns)


def search_dirs(cfg: SlicerConfig) -> list[Path]:
    """Where presets are looked up, in order: the warden's own profiles (imported or generated), the configured
    vendor, Orca's filament library, then every other vendor next to it."""
    vendor = Path(cfg.vendor_dir)
    dirs = [Path(cfg.user_dir)] if cfg.user_dir else []
    dirs.append(vendor)
    root = Path(cfg.profiles_root) if cfg.profiles_root else vendor.parent
    if root.is_dir():
        others = sorted(
            d
            for d in root.iterdir()
            if d.is_dir() and any((d / k).is_dir() for k in KINDS)
        )
        dirs += sorted(others, key=lambda d: d.name != LIBRARY)
    seen: list[Path] = []
    for d in dirs:
        if d not in seen and d.is_dir():
            seen.append(d)
    return seen


def find(
    dirs: Path | list[Path], kind: str, name: str, home: Path | None = None
) -> tuple[dict[str, Any], Path]:
    """A preset by name and the directory it came from. A parent is looked up in its child's directory first, then
    Orca's filament library, then everywhere (as Orca does: `fdm_machine_common` differs per vendor)."""
    order = [dirs] if isinstance(dirs, Path) else list(dirs)
    if home is not None:
        order = [home, *(d for d in order if d.name == LIBRARY and d != home)] + [
            d for d in order if d != home and d.name != LIBRARY
        ]
    for d in order:
        if path := _index(d, kind).get(name):
            return json.loads(Path(path).read_text()), d
    raise FileNotFoundError(f"{kind} preset not found: {name!r}")


def flatten(
    dirs: Path | list[Path],
    kind: str,
    name: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preset, home = find(dirs, kind, name)
    chain = [preset]
    while parent := chain[-1].get("inherits"):
        if len(chain) > 20:
            raise ValueError(f"{kind} preset {name!r}: inherits loops")
        preset, home = find(dirs, kind, parent, home)
        chain.append(preset)
    merged: dict[str, Any] = {}
    for p in reversed(chain):
        merged.update({k: v for k, v in p.items() if k != "inherits"})
    if str(chain[0].get("from", "")).lower() == "user":
        # Orca's CLI refuses a user preset unless it names a bundled parent, whose name its compatibility checks use.
        system = next(
            (p for p in chain if str(p.get("from")).lower() == "system"), None
        )
        if system is None:
            raise ValueError(
                f"{kind} preset {name!r} has no OrcaSlicer system preset among its parents"
            )
        merged.update({"inherits": system["name"], "from": "User"})
    merged.update(overrides or {})
    return merged


_TIME = re.compile(
    r"estimated printing time.*?=\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
    re.IGNORECASE,
)


def parse_gcode_header(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = text.splitlines()
    for line in lines[:500] + lines[-3000:]:
        if not line.startswith(";"):
            continue
        low = line.lower()
        if "estimated printing time" in low and "est_seconds" not in out:
            m = _TIME.search(line)
            if m:
                d, h, mi, s = (int(x or 0) for x in m.groups())
                out["est_seconds"] = d * 86400 + h * 3600 + mi * 60 + s
        elif "total filament used [g]" in low or (
            "filament used [g]" in low and "filament_g" not in out
        ):
            out["filament_g"] = float(line.split("=")[-1].strip().split(",")[0])
        elif "filament used [mm]" in low and "filament_m" not in out:
            out["filament_m"] = round(
                float(line.split("=")[-1].strip().split(",")[0]) / 1000, 2
            )
        elif "total layer number" in low:
            out["layers"] = int(line.split(":")[-1].split("=")[-1].strip())
        elif "max_z_height" in low:
            out["max_z"] = float(line.split(":")[-1].split("=")[-1].strip())
    return out


async def render_scad(
    openscad: str, scad: Path, out_stl: Path, defines: dict[str, Any] | None = None
) -> str:
    cmd = [openscad, "-o", str(out_stl)]
    for k, v in (defines or {}).items():
        cmd += ["-D", f"{k}={json.dumps(v)}"]
    cmd.append(str(scad))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out_stl.exists():
        raise RuntimeError(
            f"openscad failed ({proc.returncode}): {out.decode(errors='ignore')[-1500:]}"
        )
    return out.decode(errors="ignore")[-1500:]


CACHE_VERSION = 1


def _orca_identity(orca: str) -> str:
    """Path, size and mtime of the slicer binary, so an upgraded Orca never serves a stale cached slice."""
    try:
        st = Path(orca).stat()
        return f"{orca}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return orca


def cache_key(
    stl_bytes: bytes,
    configs: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    extra: dict[str, Any],
) -> str:
    h = hashlib.sha256()
    h.update(hashlib.sha256(stl_bytes).digest())
    h.update(
        json.dumps(
            [CACHE_VERSION, configs, extra], sort_keys=True, default=str
        ).encode()
    )
    return h.hexdigest()[:32]


def _from_cache(entry: Path, out_dir: Path, name: str) -> SliceResult | None:
    try:
        meta = json.loads((entry / "result.json").read_text())
    except (OSError, ValueError):
        return None
    paths: dict[str, Path | None] = {}
    for kind, suffix in (("gcode", ".gcode"), ("threemf", ".3mf"), ("preview", ".png")):
        src = entry / f"slice{suffix}"
        if src.exists():
            dst = out_dir / f"{name}{suffix}"
            shutil.copyfile(src, dst)
            os.utime(src)
            paths[kind] = dst
        else:
            paths[kind] = None
    gcode, threemf = paths["gcode"], paths["threemf"]
    if gcode is None or threemf is None:
        return None
    os.utime(entry / "result.json")
    return SliceResult(
        gcode,
        threemf,
        paths["preview"],
        meta.get("est_seconds"),
        meta.get("filament_g"),
        meta.get("filament_m"),
        meta.get("layers"),
        meta.get("max_z"),
        meta.get("warnings") or [],
        "(from slice cache)",
        meta.get("blockers") or [],
        cached=True,
    )


def _to_cache(entry: Path, res: SliceResult) -> None:
    entry.mkdir(parents=True, exist_ok=True)
    for src, suffix in (
        (res.gcode, ".gcode"),
        (res.threemf, ".3mf"),
        (res.preview, ".png"),
    ):
        if src and src.exists():
            shutil.copyfile(src, entry / f"slice{suffix}")
    fields = (
        "est_seconds",
        "filament_g",
        "filament_m",
        "layers",
        "max_z",
        "warnings",
        "blockers",
    )
    (entry / "result.json").write_text(json.dumps({f: getattr(res, f) for f in fields}))


async def slice_stl(
    cfg: SlicerConfig,
    stl: Path,
    out_dir: Path,
    machine: str,
    process: str,
    filament: str,
    overrides: dict[str, Any] | None = None,
    park: Pose | None = None,
    material: str = "",
    copies: int = 1,
    orient: bool = False,
    filament_overrides: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    project: Path | None = None,
    plate: int = 1,
    calibration: dict[str, Any] | None = None,
) -> SliceResult:
    """Raises ValueError before slicing when the model cannot fit; problems found in the sliced gcode come back as
    `blockers`, which keep the job from starting. With `cache_dir`, an identical request (same model bytes, same
    final machine/process/filament settings, copies, orient, park pose and Orca binary) reuses the earlier slice.

    With `project` (a 3MF from `threemf.sanitize`) Orca slices that plate of the project, the printer's settings
    loaded over it; `stl` is then the plate's geometry, for the fit check and naming. A stripped setting's sentinel
    in the output blocks the job when the printer's profile defines that setting (Orca's override did not hold)."""
    if project and (copies > 1 or orient):
        raise ValueError(
            "copies and orient re-arrange the plate: slice the geometry, not the project"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    vendor = search_dirs(cfg)
    house = json.loads(Path(cfg.house_rules).read_text()) if cfg.house_rules else {}
    if copies < 1 or copies > 50:
        raise ValueError("copies must be 1-50")
    machine_cfg = flatten(vendor, "machine", machine)
    if not orient and (
        problems := checks.fit_problems(checks.stl_bounds(stl), machine_cfg, copies)
    ):
        raise ValueError(
            f"{stl.name} cannot be printed on {machine}: " + "; ".join(problems)
        )
    if park:
        machine_cfg["machine_end_gcode"] = parking.prepare_end_gcode(
            str(machine_cfg.get("machine_end_gcode") or "")
        )
    process_cfg = flatten(vendor, "process", process, {**house, **(overrides or {})})
    filament_cfg = flatten(vendor, "filament", filament, filament_overrides)
    # printers.yaml pairs these presets with this printer; Orca's CLI refuses a process whose compatibility list does
    # not name the machine's bundled preset (a user's copy is matched by the bundled preset it inherits).
    if isinstance(filament_cfg.get("default_filament_colour"), str):
        # Cubicon's bundled filaments carry it as a string, which aborts Orca's CLI.
        filament_cfg["default_filament_colour"] = [
            filament_cfg["default_filament_colour"]
        ]
    target = machine_cfg.get("inherits") or machine_cfg.get("name") or machine
    for c in (process_cfg, filament_cfg):
        listed = c.get("compatible_printers")
        if isinstance(listed, list) and listed and target not in listed:
            c["compatible_printers"] = [*listed, target]
    name = stl.stem
    entry = None
    if cache_dir:
        key = cache_key(
            (project or stl).read_bytes(),
            (machine_cfg, process_cfg, filament_cfg),
            {
                "copies": copies,
                "orient": orient,
                "park": park,
                "material": material.upper(),
                "blocking": cfg.blocking_warnings,
                "orca": _orca_identity(cfg.orca),
                "plate": plate if project else None,
                "calibration": calibration,
            },
        )
        entry = cache_dir / key
        if hit := _from_cache(entry, out_dir, name):
            return hit
    tmp = Path(tempfile.mkdtemp(prefix="slice-"))
    try:
        (tmp / "machine.json").write_text(json.dumps(machine_cfg))
        (tmp / "process.json").write_text(json.dumps(process_cfg))
        (tmp / "filament.json").write_text(json.dumps(filament_cfg))
        cmd = [
            "xvfb-run",
            "-a",
            cfg.orca,
            "--load-settings",
            f"{tmp / 'machine.json'};{tmp / 'process.json'}",
            "--load-filaments",
            str(tmp / "filament.json"),
            *(["--clone-objects", str(copies)] if copies > 1 else []),
            *(["--arrange", "1"] if copies > 1 else []),
            *(["--orient", "1"] if orient else []),
            "--slice",
            str(plate) if project else "0",
            "--export-3mf",
            f"{name}.3mf",
            "--outputdir",
            str(out_dir),
            str(project or stl),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        log = out.decode(errors="ignore")
        threemf = out_dir / f"{name}.3mf"
        if not threemf.exists():
            raise RuntimeError(
                f"orca-slicer produced no 3mf (rc={proc.returncode}): {log[-2000:]}"
            )
        with zipfile.ZipFile(threemf) as z:
            names = z.namelist()
            gcodes = [n for n in names if n.lower().endswith(".gcode")]
            if not gcodes:
                raise RuntimeError(f"no gcode inside 3mf: {log[-2000:]}")
            text = z.read(gcodes[0]).decode(errors="ignore")
            leaks = project_leaks(text) if project else []
            if calibration:
                text = calibrate_gcode(
                    text,
                    calibration["kind"],
                    tuple(calibration["values"]),
                    calibration["section_h"],
                    calibration.get("reset"),
                )
            park_warnings: list[str] = []
            if park:
                height = machine_cfg.get("printable_height")
                text, park_warnings = parking.apply(
                    text,
                    park,
                    parse_gcode_header(text).get("max_z"),
                    float(height) if height else None,
                )
            gcode = out_dir / f"{name}.gcode"
            gcode.write_text(text)
            preview = None
            for cand in (
                f"Metadata/plate_{plate}.png",
                "Metadata/plate_1.png",
                "Metadata/top_1.png",
                "Metadata/pick_1.png",
            ):
                if cand in names:
                    preview = out_dir / f"{name}.png"
                    preview.write_bytes(z.read(cand))
                    break
        if preview is None:
            try:
                preview = render_preview(
                    text, checks.bed_rect(machine_cfg), out_dir / f"{name}.png"
                )
            except (ValueError, OSError):
                preview = None
        meta = parse_gcode_header(text)
        blockers = checks.gcode_problems(
            checks.gcode_facts(text), machine_cfg, material
        )
        blockers += checks.log_problems(log, cfg.blocking_warnings)
        defined = set(machine_cfg) | set(process_cfg) | set(filament_cfg)
        blockers += [
            f"the project's {k} reached the gcode although the printer profile sets it; not printing this slice"
            for k in leaks
            if k in defined
        ]
        neutral = [k for k in leaks if k not in defined]
        warnings = (
            (
                [
                    "the project set gcode the printer profile does not define; the warden emptied it: "
                    + ", ".join(neutral)
                ]
                if neutral
                else []
            )
            + park_warnings
            + [ln.strip() for ln in log.splitlines() if "warn" in ln.lower()][:10]
        )
        res = SliceResult(
            gcode,
            threemf,
            preview,
            meta.get("est_seconds"),
            meta.get("filament_g"),
            meta.get("filament_m"),
            meta.get("layers"),
            meta.get("max_z"),
            warnings,
            log[-800:],
            blockers,
        )
        if entry is not None:
            _to_cache(entry, res)
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
