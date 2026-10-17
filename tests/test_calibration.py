import json

import httpx
import pytest

from print_warden import calibration, checks, spoolman


def test_values_and_commands():
    assert calibration.values("temp", 200) == (210, 205, 200, 195, 190)
    assert calibration.values("pa", bowden=True)[0] == 0.2
    with pytest.raises(ValueError):
        calibration.values("temp", None)
    with pytest.raises(ValueError):
        calibration.values("speed")
    assert calibration.command("temp", 205) == "M104 S205"
    assert calibration.command("flow", 0.98) == "M221 S98"
    assert calibration.command("pa", 0.04) == "SET_PRESSURE_ADVANCE ADVANCE=0.040"
    assert calibration.restore("flow", None) == "M221 S100"
    assert calibration.restore("pa", 0.05) == "SET_PRESSURE_ADVANCE ADVANCE=0.050"
    assert calibration.restore("pa", None) is None
    assert calibration.restore("temp", 0.05) is None


def test_tower_steps_inward_only(tmp_path):
    stl = tmp_path / "t.stl"
    stl.write_bytes(calibration.tower_stl())
    assert checks.stl_bounds(stl) == pytest.approx((20, 20, 40))


def test_apply_uses_layer_marks_not_z_hops():
    lines = [
        ";TYPE:Custom",
        "G1 Z5 F600",
        ";LAYER_CHANGE",
        ";Z:0.2",
        ";TYPE:Outer wall",
        "G1 X1 Y1 E1",
        "G1 Z8.4",  # a z-hop near the boundary: must not switch sections
        ";LAYER_CHANGE",
        ";Z:7.8",
        "G1 X2 Y2 E1",
        ";LAYER_CHANGE",
        ";Z:8",
        "G1 X3 Y3 E1",
        ";LAYER_CHANGE",
        ";Z:39.8",
    ]
    out = calibration.apply(
        "\n".join(lines), "temp", (210, 205, 200, 195, 190), reset="M221 S100"
    )
    sections = [ln for ln in out.splitlines() if "calibration section" in ln]
    assert sections == [
        "M104 S210 ; print-warden calibration section 1",
        "M104 S205 ; print-warden calibration section 2",
        "M104 S190 ; print-warden calibration section 5",
    ]
    lines = out.splitlines()
    assert lines[lines.index(sections[1]) + 1] == ";Z:8"
    assert lines[-1] == "M221 S100 ; print-warden calibration reset"
    with pytest.raises(ValueError):
        calibration.apply("G1 X1\n", "temp", (200,))


def test_result_and_choices():
    cal = calibration.spec("flow", calibration.values("flow"), 7)
    assert calibration.result(cal, 4, {"flow_ratio": 0.95}) == ("pw_flow_ratio", 0.931)
    assert calibration.result(cal, 3, {}) == ("pw_flow_ratio", 1.0)
    with pytest.raises(ValueError):
        calibration.result(cal, 6, {})
    page1 = calibration.choices("j1", cal, 1)
    assert [a for a, _ in page1] == [
        "PRINT_WARDEN_CALIB_j1:1",
        "PRINT_WARDEN_CALIB_j1:2",
        "PRINT_WARDEN_CALIB_j1:more",
    ]
    assert page1[0][1] == "1 (bottom): 104%"
    assert [label for _, label in calibration.choices("j1", cal, 2)][
        -1
    ] == "5 (top): 96%"


async def test_spoolman_tuning_round_trip():
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(
            (r.method, r.url.path, json.loads(r.content) if r.content else None)
        )
        if r.method == "POST":
            return httpx.Response(400, json={"detail": "exists"})
        return httpx.Response(
            200,
            json={
                "id": 7,
                "filament": {"material": "PLA"},
                "extra": {"pw_flow_ratio": "0.97", "other": '"x"'},
            },
        )

    sm = spoolman.Spoolman("http://spool")
    sm._c = httpx.AsyncClient(
        base_url="http://spool/api/v1", transport=httpx.MockTransport(handler)
    )
    out = await sm.set_tuning(7, "pw_flow_ratio", 0.97)
    assert out["tuning"] == {"flow_ratio": 0.97}
    assert seen[0][:2] == ("POST", "/api/v1/field/spool/pw_flow_ratio")
    assert seen[1] == ("PATCH", "/api/v1/spool/7", {"extra": {"pw_flow_ratio": "0.97"}})
    with pytest.raises(ValueError):
        await sm.set_tuning(7, "colour", 1)
