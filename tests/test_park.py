import pytest

from print_warden import config, park
from print_warden.config import Pose

END = """G91
G1 Z5 F600
G90
M104 S0
M140 S0
M84 X Y E
M18
"""


def test_marker_goes_before_the_first_stepper_off_and_those_lines_are_dropped():
    out = park.prepare_end_gcode(END).splitlines()
    assert out.index(park.MARKER) == 5
    assert not any(ln.strip().startswith(("M84", "M18")) for ln in out)
    assert sum("removed, idle_timeout" in ln for ln in out) == 2


def test_marker_first_when_end_gcode_has_no_stepper_off():
    out = park.prepare_end_gcode("END_PRINT")
    assert out.splitlines() == [park.MARKER, "END_PRINT"]


def test_apply_fills_in_the_park_block():
    pose = Pose(y=220, z=150)
    gcode, warnings = park.apply(f"G1 X1\n{park.MARKER}\nM107\n", pose, 40.0, 400.0)
    assert warnings == []
    assert park.MARKER not in gcode
    assert "G1 Z150.00 F600" in gcode and "G1 Y220.00 F3000" in gcode
    assert gcode.index("SAVE_GCODE_STATE") < gcode.index("RESTORE_GCODE_STATE")


def test_tall_part_parks_higher_never_lower_and_never_past_the_machine():
    pose = Pose(y=220, z=150, x=10)
    assert park.park_z(pose, 200.0, 400.0) == 210.0
    assert park.park_z(pose, 395.0, 400.0) == 400.0
    gcode, warnings = park.apply(park.MARKER, pose, 200.0, 400.0)
    assert "G1 Z210.00" in gcode and "G1 X10.00 Y220.00" in gcode
    assert "above pose" in warnings[0]


def test_missing_marker_is_a_warning_not_a_crash():
    gcode, warnings = park.apply("G1 X1", Pose(y=1, z=1), 5.0, None)
    assert gcode == "G1 X1" and "marker missing" in warnings[0]


POSE = Pose(y=220, z=150)


@pytest.mark.parametrize(
    "toolhead,ok,why",
    [
        ({"homed_axes": "xyz", "position": [5, 220.4, 160, 0]}, True, "at pose"),
        ({"homed_axes": "", "position": [5, 220, 160, 0]}, False, "steppers released"),
        ({"homed_axes": "xyz", "position": [5, 100, 160, 0]}, False, "bed at Y100.0"),
        (
            {"homed_axes": "xyz", "position": [5, 220, 20, 0]},
            False,
            "below the check pose",
        ),
        ({}, False, "position unknown"),
    ],
)
def test_status(toolhead, ok, why):
    got_ok, got_why = park.status(toolhead, POSE)
    assert got_ok is ok and why in got_why


def test_trusted_z_stands_in_after_a_y_only_rehome():
    th = {"homed_axes": "y", "position": [0, 220, 160, 0]}
    assert not park.status(th, POSE)[0]
    assert park.status(th, POSE, trusted_z=160)[0]
    assert not park.status(th, POSE, trusted_z=20)[0]


def test_x_in_pose_needs_x_homed():
    pose = Pose(y=220, z=150, x=10)
    assert not park.status({"homed_axes": "yz", "position": [10, 220, 160, 0]}, pose)[0]
    assert park.status({"homed_axes": "xyz", "position": [10, 220, 160, 0]}, pose)[0]


def test_rehome_never_moves_z():
    script = park.rehome_script(POSE)
    assert script.startswith("G28 Y") and "Z" not in script
    assert park.rehome_script(Pose(y=1, z=1, x=2)).startswith("G28 X Y")


def test_rehome_requires_a_pose():
    with pytest.raises(ValueError, match="rehome_y needs"):
        config._bed_check({"rehome_y": True})
    bc = config._bed_check({"pose": {"y": 220, "z": 150}, "rehome_y": True})
    assert bc.pose == Pose(y=220, z=150) and bc.rehome_y


X4_PLUS_END = """G91 ;Relative positioning
G1 E-1 F2700 ;Retract a bit
G1 E-1 Z0.2 F2400 ;Retract and raise Z
G1 X5 Y5 F3000 ;Wipe out
G1 Z1 ;Raise Z more
G90 ;Absolute positionning
G1 X-5 Y305 F3000 ;Wipe out
M106 S0 ;Turn-off fan
M104 S0 ;Turn-off hotend
M140 S0 ;Turn-off bed
M84 X Y E ;Disable all steppers but Z"""

X1_END_TAIL = """M104 S0 ; turn-off hotend
M140 S0 ; turn-off bed
M150 P0 ; turn off led
M85 S0 ; deactivate idle timeout
M84 ; disable motors
"""


@pytest.mark.parametrize("end", [X4_PLUS_END, X1_END_TAIL])
def test_real_artillery_end_gcode(end):
    """OrcaSlicer 2.3.1's Artillery Sidewinder X4 Plus / X1 machine profiles."""
    lines = park.prepare_end_gcode(end).splitlines()
    assert lines[-2] == park.MARKER
    assert lines[-1].startswith("; print-warden: 'M84")
    gcode, _ = park.apply("\n".join(lines), Pose(y=220, z=150), 30.0, 400.0)
    tail = gcode.splitlines()[-8:]
    assert tail[0] == "; print-warden: park for the bed check"
    assert "G1 Y220.00 F3000" in tail
