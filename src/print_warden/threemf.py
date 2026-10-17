"""3MF projects: geometry as STL, and the project itself with everything that can run gcode stripped.

Two ways to slice a 3MF, both keeping the project's own gcode away from the printer:

- **Project mode** (`sanitize`): the project goes to OrcaSlicer whole, so modifiers, negative parts and per-object
  settings work, with the printer's machine, process and filament settings loaded over it. Before that, every
  setting whose name says gcode (start/end, layer change, filament change, timelapse, ...) and `post_process` is
  emptied in `project_settings.config` and in per-object metadata, and embedded plate gcode is dropped. The lines
  that were removed are returned so the slice can be refused if any of them shows up in the output.
- **Geometry mode** (`to_stl`, below): one plate becomes one binary STL for the plain STL path. Used for the fit
  check, the model hash, and slicing with `copies`/`orient`, which need Orca to re-arrange the plate.

A 3MF (OrcaSlicer, Bambu Studio, PrusaSlicer, or any 3MF core writer) is read for its meshes and nothing else: one plate
becomes one binary STL, which then goes through the same slice path as an uploaded STL (house rules, fit and gcode
checks, cache, park). The project's own print settings, start gcode and painted supports are deliberately ignored,
so a downloaded project cannot bring its own gcode to the printer.

- Meshes follow the 3MF core spec (``<mesh>`` vertices/triangles, ``<components>`` with row-major 4x3 transforms,
  ``<build>`` items) and the production extension's ``p:path`` for components in other model files.
- Plates come from ``Metadata/model_settings.config`` (Orca/Bambu); without one the whole build is plate 1. Items
  marked ``printable="0"`` are skipped.
- Orca/Bambu part subtypes: modifiers and support enforcers/blockers carry settings, not material, and are dropped
  with a warning; negative parts would silently fill holes, so a plate that has one is refused.
- The plate is moved so its footprint is centred on the origin and it rests on z = 0; relative positions are kept.
- Limits: 256 MB per model file once decompressed, 5 million triangles per plate.
"""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
UNITS = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TRIANGLES = 5_000_000
DROPPED_SUBTYPES = ("modifier_part", "support_blocker", "support_enforcer")

Matrix = tuple[float, ...]
IDENTITY: Matrix = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
Triangle = tuple[tuple[float, float, float], ...]


@dataclass
class PlateResult:
    stl: Path
    plate: int
    plates: list[int]
    objects: int
    triangles: int
    warnings: list[str] = field(default_factory=list)


def _matrix(text: str | None) -> Matrix:
    if not text:
        return IDENTITY
    m = tuple(float(v) for v in text.split())
    if len(m) != 12:
        raise ValueError(f"3mf transform has {len(m)} values, expected 12")
    return m


def _compose(inner: Matrix, outer: Matrix) -> Matrix:
    """The transform that applies `inner` first, then `outer` (3MF row vectors: p' = p · M)."""
    a, b = inner, outer
    rows = [(a[0], a[1], a[2]), (a[3], a[4], a[5]), (a[6], a[7], a[8])]
    out: list[float] = []
    for r in rows:
        for c in range(3):
            out.append(r[0] * b[c] + r[1] * b[3 + c] + r[2] * b[6 + c])
    for c in range(3):
        out.append(a[9] * b[c] + a[10] * b[3 + c] + a[11] * b[6 + c] + b[9 + c])
    return tuple(out)


def _apply(m: Matrix, x: float, y: float, z: float) -> tuple[float, float, float]:
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


class _Package:
    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.models: dict[str, ET.Element] = {}

    def model(self, path: str) -> ET.Element:
        path = path.lstrip("/")
        if path not in self.models:
            try:
                info = self.z.getinfo(path)
            except KeyError:
                raise ValueError(f"3mf references a missing part {path!r}") from None
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"3mf part {path} exceeds {MAX_FILE_BYTES >> 20} MB")
            self.models[path] = ET.fromstring(self.z.read(info))
        return self.models[path]

    def root_path(self) -> str:
        try:
            rels = ET.fromstring(self.z.read("_rels/.rels"))
            for r in rels:
                if r.get("Type", "").endswith("/3dmodel"):
                    return r.get("Target", "").lstrip("/")
        except KeyError:
            pass
        return "3D/3dmodel.model"


def _objects(model: ET.Element) -> dict[str, ET.Element]:
    res = model.find(f"{CORE}resources")
    return {o.get("id", ""): o for o in (res if res is not None else [])}


def _part_subtypes(z: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    """(object id, part id) → Orca/Bambu subtype, from model_settings.config."""
    try:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    except KeyError:
        return {}
    return {
        (o.get("id", ""), p.get("id", "")): p.get("subtype", "normal_part")
        for o in cfg.iter("object")
        for p in o.iter("part")
    }


def _plates(z: zipfile.ZipFile) -> dict[int, set[tuple[str, int]]]:
    """Plate number → the (object id, instance index) build items on it; empty without a plate config."""
    try:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    except KeyError:
        return {}
    plates: dict[int, set[tuple[str, int]]] = {}
    for plate in cfg.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        n = int(meta.get("plater_id") or len(plates) + 1)
        items = set()
        for inst in plate.iter("model_instance"):
            im = {m.get("key"): m.get("value") for m in inst.findall("metadata")}
            items.add((im.get("object_id") or "", int(im.get("instance_id") or 0)))
        plates[n] = items
    return plates


class _Collector:
    def __init__(
        self,
        pkg: _Package,
        subtypes: dict[tuple[str, str], str],
        allow_negative: bool = False,
    ):
        self.pkg = pkg
        self.subtypes = subtypes
        self.allow_negative = allow_negative
        self.triangles: list[Triangle] = []
        self.dropped: dict[str, int] = {}

    def walk(
        self,
        path: str,
        obj_id: str,
        m: Matrix,
        scale: float,
        top_id: str | None,
        depth: int = 0,
    ) -> None:
        if depth > 16:
            raise ValueError("3mf components nest too deeply")
        model = self.pkg.model(path)
        obj = _objects(model).get(obj_id)
        if obj is None:
            raise ValueError(f"3mf object {obj_id} not found in {path}")
        if obj.get("type", "model") not in ("model", "solidsupport"):
            return
        mesh = obj.find(f"{CORE}mesh")
        if mesh is not None:
            verts = [
                _apply(
                    m,
                    float(v.get("x", 0)) * scale,
                    float(v.get("y", 0)) * scale,
                    float(v.get("z", 0)) * scale,
                )
                for v in mesh.iterfind(f"{CORE}vertices/{CORE}vertex")
            ]
            for t in mesh.iterfind(f"{CORE}triangles/{CORE}triangle"):
                try:
                    tri = tuple(verts[int(t.get(k, -1))] for k in ("v1", "v2", "v3"))
                except IndexError:
                    raise ValueError(
                        f"3mf object {obj_id} has a triangle with a bad vertex index"
                    ) from None
                self.triangles.append(tri)
            if len(self.triangles) > MAX_TRIANGLES:
                raise ValueError(f"plate has more than {MAX_TRIANGLES} triangles")
        comps = obj.find(f"{CORE}components")
        for c in comps if comps is not None else []:
            cid = c.get("objectid", "")
            if top_id is not None:
                sub = self.subtypes.get((top_id, cid), "normal_part")
                if sub == "negative_part" and self.allow_negative:
                    continue
                if sub == "negative_part":
                    raise ValueError(
                        "the plate has a negative part (a volume subtracted from the model), which the warden "
                        "cannot apply when slicing geometry only; export the plate as STL from the slicer instead"
                    )
                if sub in DROPPED_SUBTYPES:
                    self.dropped[sub] = self.dropped.get(sub, 0) + 1
                    continue
            cpath = c.get(f"{PROD}path") or path
            self.walk(
                cpath,
                cid,
                _compose(_matrix(c.get("transform")), m),
                _scale(self.pkg.model(cpath)),
                None,
                depth + 1,
            )


def _scale(model: ET.Element) -> float:
    unit = model.get("unit", "millimeter")
    if unit not in UNITS:
        raise ValueError(f"3mf unit {unit!r} is not supported")
    return UNITS[unit]


def _normal(t: Triangle) -> tuple[float, float, float]:
    """Unit facet normal; OrcaSlicer refuses a binary STL whose normals are all zero."""
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz, vx, vy, vz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
    return (n[0] / length, n[1] / length, n[2] / length) if length else (0.0, 0.0, 1.0)


def plate_numbers(path: Path) -> list[int]:
    with zipfile.ZipFile(path) as z:
        return sorted(_plates(z)) or [1]


def to_stl(
    src: Path, out: Path, plate: int = 1, allow_negative: bool = False
) -> PlateResult:
    """Write plate `plate` of the 3MF at `src` as a binary STL at `out`. With `allow_negative` a negative part is
    left out instead of refused (for bounds and hashing, when the project itself is sliced)."""
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ValueError(f"{src.name} is not a 3mf (zip) file") from None
    with z:
        pkg = _Package(z)
        root_path = pkg.root_path()
        root = pkg.model(root_path)
        plates = _plates(z)
        numbers = sorted(plates) or [1]
        if plate not in numbers:
            raise ValueError(
                f"{src.name} has plates {numbers}; there is no plate {plate}"
            )
        wanted = plates.get(plate)
        collector = _Collector(pkg, _part_subtypes(z), allow_negative)
        build = root.find(f"{CORE}build")
        seen: dict[str, int] = {}
        objects = 0
        for item in build if build is not None else []:
            oid = item.get("objectid", "")
            instance = seen.get(oid, 0)
            seen[oid] = instance + 1
            if item.get("printable") == "0":
                continue
            if wanted is not None and (oid, instance) not in wanted:
                continue
            collector.walk(
                item.get(f"{PROD}path") or root_path,
                oid,
                _matrix(item.get("transform")),
                _scale(root),
                oid,
            )
            objects += 1
    tris = collector.triangles
    if not tris:
        raise ValueError(f"plate {plate} of {src.name} has no printable geometry")
    xs = [p[0] for t in tris for p in t]
    ys = [p[1] for t in tris for p in t]
    zs = [p[2] for t in tris for p in t]
    dx, dy, dz = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, min(zs)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        f.write(f"print-warden: {src.name} plate {plate}".encode()[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", *_normal(t)))
            for x, y, zz in t:
                f.write(struct.pack("<3f", x - dx, y - dy, zz - dz))
            f.write(b"\0\0")
    warnings = [
        f"{n} {sub.replace('_', ' ')} volume(s) ignored: the project's per-part settings are not used"
        for sub, n in sorted(collector.dropped.items())
    ]
    if len(numbers) > 1:
        warnings.append(
            f"{src.name} has plates {numbers}; this job is plate {plate} (submit others with plate=N)"
        )
    return PlateResult(out, plate, numbers, objects, len(tris), warnings)


STRIPPED_KEYS = ("post_process",)
NOT_SCRIPTS = ("emit_machine_limits_to_gcode",)
SENTINEL = "; print-warden stripped "


def _gcode_key(key: str) -> bool:
    """A setting that holds gcode to run: Orca's `*_gcode` scripts (not flags like `gcode_flavor`)."""
    return (key.endswith("_gcode") and key not in NOT_SCRIPTS) or key in STRIPPED_KEYS


def _blank(key: str, value: Any) -> Any:
    """The sentinel in place of a gcode setting that had content (a list keeps its length); an empty one, and
    post_process, are simply emptied."""
    if key in STRIPPED_KEYS:
        return [] if isinstance(value, list) else ""
    if not any(v.strip() for v in _strings(value)):
        return [""] * len(value) if isinstance(value, list) else ""
    mark = SENTINEL + key
    return [mark] * len(value) if isinstance(value, list) else mark


def _strings(value: Any) -> list[str]:
    return [
        v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)
    ]


@dataclass
class Sanitized:
    path: Path
    stripped_keys: list[str]


def sanitize(src: Path, out: Path) -> Sanitized:
    """Copy the project to `out` with every gcode-bearing setting replaced by a sentinel comment and embedded gcode
    dropped. OrcaSlicer loads the printer's settings over the project's, so no sentinel should reach the output;
    `leaked` finds any that did."""
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ValueError(f"{src.name} is not a 3mf (zip) file") from None
    keys: list[str] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
        for info in z.infolist():
            name = info.filename
            if name.lower().endswith((".gcode", ".gcode.md5", ".bgcode")):
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"3mf part {name} exceeds {MAX_FILE_BYTES >> 20} MB")
            data = z.read(info)
            if name == "Metadata/project_settings.config":
                try:
                    cfg = json.loads(data)
                except ValueError:
                    raise ValueError("project_settings.config is not JSON") from None
                for k, v in list(cfg.items()):
                    if _gcode_key(k):
                        keys.append(k)
                        cfg[k] = _blank(k, v)
                data = json.dumps(cfg).encode()
            elif name == "Metadata/model_settings.config":
                root = ET.fromstring(data)
                for parent in root.iter():
                    for meta in list(parent.findall("metadata")):
                        if _gcode_key(meta.get("key", "")):
                            keys.append(f"object:{meta.get('key')}")
                            parent.remove(meta)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            o.writestr(info, data)
    return Sanitized(out, sorted(set(keys)))


def leaked(gcode: str) -> list[str]:
    """Project settings that reached the sliced gcode despite the printer's being loaded over them."""
    return sorted(
        {
            ln.strip().removeprefix(SENTINEL)
            for ln in gcode.splitlines()
            if ln.strip().startswith(SENTINEL)
        }
    )
