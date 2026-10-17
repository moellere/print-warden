from PIL import Image

from print_warden import preview

GCODE = """M83
G1 E5 F300 ; prime at an unknown position
G1 X10 Y10 Z0.2
G1 X20 Y10 E.5
G1 X20 Y20 E.5
G1 E-.8
G1 X50 Y50
G1 Z0.4
G1 X60 Y50 E.4
"""


def test_segments_need_a_known_position_and_positive_extrusion():
    segs = preview.segments(GCODE)
    assert segs == [(10, 10, 20, 10, 0.2), (20, 10, 20, 20, 0.2), (50, 50, 60, 50, 0.4)]


def test_absolute_e():
    segs = preview.segments(
        "M82\nG92 E0\nG1 X0 Y0\nG1 X5 Y0 E1\nG1 X9 Y0 E0.5\nG1 X9 Y9 E2\n"
    )
    assert segs == [(0, 0, 5, 0, 0.0), (9, 0, 9, 9, 0.0)]


def test_render_draws_on_the_bed(tmp_path):
    out = preview.render(GCODE, (0, 0, 300, 310), tmp_path / "p.png")
    im = Image.open(out)
    assert abs(im.width / im.height - 300 / 310) < 0.05
    assert len(im.getcolors(maxcolors=4096) or []) > 3
    assert preview.render("G28\n", (0, 0, 300, 300), tmp_path / "none.png") is None
