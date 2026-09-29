"""Headless OrcaSlicer wrapper. Presets are flattened from the vendor profile directory (Orca's CLI
cannot resolve `inherits`), house rules are applied last, and the gcode is pulled out of the 3mf."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import SlicerConfig


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


def _load_named(vendor: Path, kind: str, name: str) -> dict[str, Any]:
    p = vendor / kind / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"preset not found: {p}")
    return json.loads(p.read_text())


def flatten(
    vendor: Path, kind: str, name: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    preset = _load_named(vendor, kind, name)
    chain = [preset]
    while chain[-1].get("inherits"):
        chain.append(_load_named(vendor, kind, chain[-1]["inherits"]))
    merged: dict[str, Any] = {}
    for p in reversed(chain):
        merged.update({k: v for k, v in p.items() if k != "inherits"})
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


async def slice_stl(
    cfg: SlicerConfig,
    stl: Path,
    out_dir: Path,
    machine: str,
    process: str,
    filament: str,
    overrides: dict[str, Any] | None = None,
) -> SliceResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    vendor = Path(cfg.vendor_dir)
    house = json.loads(Path(cfg.house_rules).read_text()) if cfg.house_rules else {}
    tmp = Path(tempfile.mkdtemp(prefix="slice-"))
    try:
        (tmp / "machine.json").write_text(
            json.dumps(flatten(vendor, "machine", machine))
        )
        (tmp / "process.json").write_text(
            json.dumps(
                flatten(vendor, "process", process, {**house, **(overrides or {})})
            )
        )
        (tmp / "filament.json").write_text(
            json.dumps(flatten(vendor, "filament", filament))
        )
        name = stl.stem
        cmd = [
            "xvfb-run",
            "-a",
            cfg.orca,
            "--load-settings",
            f"{tmp / 'machine.json'};{tmp / 'process.json'}",
            "--load-filaments",
            str(tmp / "filament.json"),
            "--slice",
            "0",
            "--export-3mf",
            f"{name}.3mf",
            "--outputdir",
            str(out_dir),
            str(stl),
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
            gcode = out_dir / f"{name}.gcode"
            gcode.write_text(text)
            preview = None
            for cand in (
                "Metadata/plate_1.png",
                "Metadata/top_1.png",
                "Metadata/pick_1.png",
            ):
                if cand in names:
                    preview = out_dir / f"{name}.png"
                    preview.write_bytes(z.read(cand))
                    break
        meta = parse_gcode_header(text)
        warnings = [ln.strip() for ln in log.splitlines() if "warn" in ln.lower()][:10]
        return SliceResult(
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
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
