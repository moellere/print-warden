import struct

from print_warden import checks

X4 = {"printable_area": ["0x0", "300x0", "300x310", "0x310"], "printable_height": "400"}


def _binary_stl(path, dx, dy, dz):
    tris = [((0, 0, 0), (dx, 0, 0), (0, dy, dz))]
    data = b"\0" * 80 + struct.pack("<I", len(tris))
    for a, b, c in tris:
        data += struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0)
    path.write_bytes(data)
    return path


def test_stl_bounds_binary_and_ascii(tmp_path):
    assert checks.stl_bounds(_binary_stl(tmp_path / "b.stl", 20, 30, 40)) == (
        20,
        30,
        40,
    )
    (tmp_path / "a.stl").write_text(
        "solid t\nfacet normal 0 0 1\nouter loop\nvertex -5 0 0\nvertex 5 2 0\nvertex 0 0 7.5\n"
        "endloop\nendfacet\nendsolid t\n"
    )
    assert checks.stl_bounds(tmp_path / "a.stl") == (10, 2, 7.5)


def test_fit_problems():
    assert checks.fit_problems((200, 300, 100), X4) == []
    assert checks.fit_problems((305, 290, 10), X4) == []  # fits rotated
    assert "footprint" in checks.fit_problems((320, 320, 10), X4)[0]
    assert "height" in checks.fit_problems((10, 10, 410), X4)[0]
    assert checks.fit_problems((50, 50, 10), X4, copies=4) == []
    assert "copies" in checks.fit_problems((150, 150, 10), X4, copies=4)[0]


GCODE = """M140 S60
M104 S215
G28
M83
G1 X10 Y10 F3000 ; travel, no extrusion
G1 X50 Y20 E1.5
G1 E-0.8 ; retract
G1 X290 Y300 ; travel
G1 X280 Y305 E2
M104 S0
M140 S0
"""


def test_gcode_facts_relative_e():
    facts = checks.gcode_facts(GCODE)
    assert facts["extents"] == (50, 20, 280, 305)
    assert facts["nozzle"] == [215] and facts["bed"] == [60]


def test_gcode_facts_absolute_e_counts_only_forward_moves():
    text = "M82\nG92 E0\nG1 X5 Y5 E1\nG1 X100 Y100 E0.5\nG1 X40 Y40 E2\n"
    assert checks.gcode_facts(text)["extents"] == (5, 5, 40, 40)


def test_gcode_problems():
    assert checks.gcode_problems(checks.gcode_facts(GCODE), X4, "PLA") == []
    off_bed = checks.gcode_facts("M83\nG1 X320 Y10 E1\n")
    assert "outside the bed" in checks.gcode_problems(off_bed, X4, "PLA")[0]
    probs = checks.gcode_problems(checks.gcode_facts(GCODE), X4, "ABS")
    assert any("nozzle 215" in p for p in probs) and any("bed 60" in p for p in probs)
    assert checks.gcode_problems(checks.gcode_facts(GCODE), X4, "UNKNOWN") == []


def test_log_problems():
    log = "ok\nWARNING: Empty layer between 2.0 and 2.2\nall good\n"
    assert checks.log_problems(log, ("empty layer",)) == [
        "slicer: WARNING: Empty layer between 2.0 and 2.2"
    ]
    assert checks.log_problems(log, ()) == []
