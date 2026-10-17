"""Did the part stay on the bed: the first layer the gcode drew, projected into the bed camera, against what changed.

Klipper reports a print complete even when the part came off the plate halfway. After a completed print the warden
already takes a bed-check frame at the parked pose; with a camera calibration it can also say where on that frame the
part should be.

- Calibration: the image positions (fractions of width and height) of the printable area's four corners, seen at
  the bed-check pose, in the order front-left, front-right, back-right, back-left (x0y0, x1y0, x1y1, x0y1 in bed
  coordinates). A homography maps bed millimetres to the image.
- Footprint: every first-layer extrusion move of the job's gcode, sampled along its length, projected and widened a
  little. The base of a part is where it touches the bed, so parallax from its height does not move it.
- Verdict: of the footprint, how much differs from the empty-bed reference (`bedcheck.diff_mask`), and of the bed
  outside it. Little change inside means the part is not there; a lot of change outside means something else is on
  the bed (a shifted part, spaghetti). Either becomes a hint on the outcome prompt; a person still decides.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

from .bedcheck import SIZE, diff_mask
from .checks import AXIS

Point = tuple[float, float]
Homography = tuple[float, ...]
PRESENT_MIN = 0.25
OUTSIDE_MAX = 0.06


def homography(src: list[Point], dst: list[Point]) -> Homography:
    """The 3x3 projective map (h33 = 1) taking four `src` points to four `dst` points."""
    if len(src) != 4 or len(dst) != 4:
        raise ValueError("a homography needs exactly four point pairs")
    rows: list[list[float]] = []
    for (x, y), (u, v) in zip(src, dst, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    n = 8
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("the four corners are degenerate (three in a line?)")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(n):
            if r != col:
                f = rows[r][col] / rows[col][col]
                rows[r] = [a - f * b for a, b in zip(rows[r], rows[col], strict=True)]
    return (*(rows[i][n] / rows[i][i] for i in range(n)), 1.0)


def project(h: Homography, x: float, y: float) -> Point:
    w = h[6] * x + h[7] * y + h[8]
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


def calibration(
    corners: list[Point], bed: tuple[float, float, float, float]
) -> Homography:
    """From the four corners' image fractions to a bed-mm → image-fraction map."""
    if len(corners) != 4 or not all(0 <= u <= 1 and 0 <= v <= 1 for u, v in corners):
        raise ValueError("give four corners as [u, v] fractions of the image (0-1)")
    x0, y0, x1, y1 = bed
    return homography([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], list(corners))


NOT_PART = ("custom", "skirt", "prime tower", "wipe tower")


def first_layer(gcode: str, step_mm: float = 1.0, band_mm: float = 0.5) -> list[Point]:
    """Points along the part extrusion within `band_mm` of the lowest height a part extrudes at: the first layers,
    which share the part's footprint on the bed. Moves Orca marks as start/end gcode, skirt or prime tower (the purge
    line among them) are left out; gcode with no `;TYPE:` marks keeps every extrusion move."""
    x = y = z = None
    kind = ""
    moves: list[tuple[float, float, float, float, float]] = []
    for raw in gcode.splitlines():
        if raw.startswith(";TYPE:"):
            kind = raw[6:].strip().lower()
            continue
        line = raw.split(";", 1)[0].strip()
        if not line.startswith(("G0", "G1")):
            continue
        axes = {k.upper(): float(v) for k, v in AXIS.findall(line[2:])}
        nx, ny, nz = axes.get("X", x), axes.get("Y", y), axes.get("Z", z)
        if (
            axes.get("E", 0) > 0
            and kind not in NOT_PART
            and x is not None
            and y is not None
            and nx is not None
            and ny is not None
            and nz is not None
            and (nx, ny) != (x, y)
        ):
            moves.append((x, y, nx, ny, nz))
        x, y, z = nx, ny, nz
    if not moves:
        return []
    top = min(m[4] for m in moves) + band_mm
    out: list[Point] = []
    for ax, ay, bx, by, mz in moves:
        if mz > top:
            continue
        n = max(1, int((((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5) / step_mm))
        out += [(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n + 1)]
    return out


@dataclass(frozen=True)
class Verdict:
    inside: float
    outside: float
    parts: int
    missing: int
    hint: str | None


def _mask(points: list[Point], grow: int) -> Image.Image:
    m = Image.new("L", SIZE, 0)
    d = ImageDraw.Draw(m)
    for u, v in points:
        px, py = u * SIZE[0], v * SIZE[1]
        d.ellipse((px - 1, py - 1, px + 1, py + 1), fill=255)
    return m.filter(ImageFilter.MaxFilter(grow)) if grow > 1 else m


def _count(img: Image.Image) -> int:
    return img.histogram()[255]


def _components(mask: Image.Image) -> list[set[tuple[int, int]]]:
    """8-connected regions of the mask, largest first."""
    w, h = mask.size
    px = mask.load()
    assert px is not None
    seen: set[tuple[int, int]] = set()
    out: list[set[tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if px[x, y] != 255 or (x, y) in seen:
                continue
            comp: set[tuple[int, int]] = set()
            stack = [(x, y)]
            seen.add((x, y))
            while stack:
                cx, cy = stack.pop()
                comp.add((cx, cy))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if (
                            0 <= nx < w
                            and 0 <= ny < h
                            and (nx, ny) not in seen
                            and px[nx, ny] == 255
                        ):
                            seen.add((nx, ny))
                            stack.append((nx, ny))
            out.append(comp)
    return sorted(out, key=len, reverse=True)


def analyse(
    reference: bytes,
    current: bytes,
    h: Homography,
    footprint: list[Point],
    bed: tuple[float, float, float, float],
) -> Verdict:
    """Per part (a region of the footprint big enough to be one; purge lines are not), how much of it changed; and
    how much of the bed away from the parts changed. A hint when a part is not seen or the bed is cluttered."""
    if not footprint:
        raise ValueError("no first-layer extrusion in the gcode")
    changed = diff_mask(reference, current)
    cpx = changed.load()
    assert cpx is not None
    fp = _mask([project(h, x, y) for x, y in footprint], 3)
    comps = _components(fp)
    big = [c for c in comps if len(c) >= 9]
    seen = [sum(1 for x, y in c if cpx[x, y] == 255) / len(c) for c in big]
    missing = sum(1 for f in seen if f < PRESENT_MIN)
    x0, y0, x1, y1 = bed
    bed_px = Image.new("L", SIZE, 0)
    ImageDraw.Draw(bed_px).polygon(
        [
            (u * SIZE[0], v * SIZE[1])
            for u, v in (
                project(h, *c) for c in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            )
        ],
        fill=255,
    )
    near = fp.filter(ImageFilter.MaxFilter(9))
    blank = Image.new("L", SIZE, 0)
    rest = Image.composite(bed_px, blank, near.point(lambda v: 0 if v else 255))
    outside = _count(Image.composite(changed, blank, rest)) / max(1, _count(rest))
    inside = min(seen) if seen else 0.0
    hint = None
    if missing:
        hint = (
            f"probably failed: {missing} of {len(big)} part(s) not seen where the first layer was drawn "
            f"(least changed {inside:.0%}; missing or detached)"
        )
    elif outside > OUTSIDE_MAX:
        hint = f"check it: {outside:.0%} of the bed away from the parts changed (debris, spaghetti or a shifted part)"
    return Verdict(round(inside, 3), round(outside, 3), len(big), missing, hint)
