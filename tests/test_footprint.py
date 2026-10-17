import io

import pytest
from PIL import Image, ImageDraw

from print_warden import footprint

BED = (0.0, 0.0, 300.0, 300.0)
CORNERS = [(0.2, 0.9), (0.8, 0.9), (0.7, 0.3), (0.3, 0.3)]
W, H = 640, 480


def _h():
    return footprint.calibration(CORNERS, BED)


def _frame(parts=(), blobs=()):
    im = Image.new("RGB", (W, H), (40, 40, 40))
    d = ImageDraw.Draw(im)
    h = _h()
    quad = [
        footprint.project(h, x, y) for x, y in ((0, 0), (300, 0), (300, 300), (0, 300))
    ]
    d.polygon([(u * W, v * H) for u, v in quad], fill=(120, 120, 120))
    for x0, y0, x1, y1 in parts:
        pts = [
            footprint.project(h, x, y)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ]
        d.polygon([(u * W, v * H) for u, v in pts], fill=(200, 30, 30))
    for x, y in blobs:
        u, v = footprint.project(h, x, y)
        d.ellipse((u * W - 25, v * H - 25, u * W + 25, v * H + 25), fill=(20, 160, 220))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _square_gcode(x0, y0, x1, y1, z=0.2):
    lines = [
        ";TYPE:Custom",
        "G90",
        "M83",
        "G1 X5 Y5 Z0.3",
        "G1 X100 Y5 E5",
        ";TYPE:Outer wall",
        f"G1 Z{z}",
        f"G1 X{x0} Y{y0}",
    ]
    y = y0
    while y <= y1:
        lines += [f"G1 X{x1} Y{y} E1", f"G1 X{x0} Y{y + 1} E1"]
        y += 2
    lines += ["G1 Z1.0", "G1 X250 Y250", "G1 X260 Y260 E1"]
    return "\n".join(lines)


def test_homography_maps_the_corners():
    h = _h()
    for (x, y), (u, v) in zip(
        ((0, 0), (300, 0), (300, 300), (0, 300)), CORNERS, strict=True
    ):
        pu, pv = footprint.project(h, x, y)
        assert (pu, pv) == pytest.approx((u, v), abs=1e-9)
    with pytest.raises(ValueError):
        footprint.calibration([(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.4, 0.4)], BED)
    with pytest.raises(ValueError):
        footprint.calibration([(0.1, 0.1)] * 3, BED)


def test_first_layer_is_the_part_without_the_purge_line_or_higher_layers():
    pts = footprint.first_layer(_square_gcode(100, 100, 140, 140))
    xs = [x for x, _ in pts]
    assert min(xs) >= 100
    assert max(xs) <= 140
    unmarked = footprint.first_layer(
        _square_gcode(100, 100, 140, 140).replace(";TYPE:", "; was ")
    )
    assert min(x for x, _ in unmarked) <= 6


def test_part_present_missing_and_debris():
    fp = footprint.first_layer(_square_gcode(100, 100, 140, 140))
    ref = _frame()
    present = footprint.analyse(
        ref, _frame(parts=[(100, 100, 140, 140), (5, 4, 100, 6)]), _h(), fp, BED
    )
    assert present.hint is None
    assert present.inside > 0.5
    missing = footprint.analyse(ref, _frame(), _h(), fp, BED)
    assert missing.hint.startswith("probably failed: 1 of 1")
    debris = footprint.analyse(
        ref,
        _frame(
            parts=[(100, 100, 140, 140), (5, 4, 100, 6)],
            blobs=[(220, 60), (60, 220), (230, 230), (200, 160)],
        ),
        _h(),
        fp,
        BED,
    )
    assert debris.hint.startswith("check it")
    with pytest.raises(ValueError):
        footprint.analyse(ref, ref, _h(), [], BED)


def test_one_of_two_parts_missing():
    gcode = _square_gcode(60, 60, 100, 100) + "\n" + _square_gcode(180, 180, 220, 220)
    fp = footprint.first_layer(gcode)
    one = footprint.analyse(_frame(), _frame(parts=[(60, 60, 100, 100)]), _h(), fp, BED)
    assert (one.parts, one.missing) == (2, 1)
    both = footprint.analyse(
        _frame(),
        _frame(parts=[(60, 60, 100, 100), (180, 180, 220, 220)]),
        _h(),
        fp,
        BED,
    )
    assert both.missing == 0
    assert both.hint is None
