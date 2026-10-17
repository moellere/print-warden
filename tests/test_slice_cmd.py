import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from print_warden import slicer
from print_warden.config import Pose, SlicerConfig
from tests.test_checks import _binary_stl


@pytest.fixture
def vendor(tmp_path):
    for kind, body in {
        "machine": {
            "printable_area": ["0x0", "300x0", "300x300", "0x300"],
            "printable_height": "400",
            "machine_end_gcode": "M104 S0\nM84",
        },
        "process": {},
        "filament": {},
    }.items():
        (tmp_path / "v" / kind).mkdir(parents=True)
        (tmp_path / "v" / kind / "p.json").write_text(json.dumps(body))
    return tmp_path


@pytest.fixture
def fake_orca(monkeypatch):
    calls = {}

    async def exec_(*cmd, **kw):
        calls["cmd"] = cmd
        calls["runs"] = calls.get("runs", 0) + 1
        calls["filament"] = json.loads(
            Path(cmd[cmd.index("--load-filaments") + 1]).read_text()
        )
        out_dir = cmd[cmd.index("--outputdir") + 1]
        name = cmd[cmd.index("--export-3mf") + 1]
        machine_path, process_path = cmd[cmd.index("--load-settings") + 1].split(";")
        machine = json.loads(Path(machine_path).read_text())
        calls["machine"] = machine
        calls["process"] = json.loads(Path(process_path).read_text())
        gcode = (
            "; estimated printing time (normal mode) = 1h\n; max_z_height: 12.00\nM104 S210\nM140 S60\nM83\n"
            + calls.get("body", "G1 X10 Y10 E1\nG1 X100 Y120 E1\n")
            + machine["machine_end_gcode"]
        )
        with zipfile.ZipFile(f"{out_dir}/{name}", "w") as z:
            z.writestr("Metadata/plate_1.gcode", gcode)

        class P:
            returncode = 0

            async def communicate(self):
                return b"WARNING: Empty layer between 1 and 2\n" if calls.get(
                    "warn"
                ) else b"", None

        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    return calls


def _cfg(vendor):
    return SlicerConfig(orca="orca", vendor_dir=str(vendor / "v"), house_rules=None)


async def test_copies_and_orient_reach_the_cli(vendor, fake_orca, tmp_path):
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    res = await slicer.slice_stl(
        _cfg(vendor),
        stl,
        tmp_path / "out",
        "p",
        "p",
        "p",
        material="PLA",
        copies=3,
        orient=True,
    )
    cmd = fake_orca["cmd"]
    assert (
        cmd[cmd.index("--clone-objects") + 1] == "3"
        and "--arrange" in cmd
        and "--orient" in cmd
    )
    assert res.blockers == [] and res.est_seconds == 3600


async def test_single_copy_has_no_clone_flags(vendor, fake_orca, tmp_path):
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    await slicer.slice_stl(
        _cfg(vendor), stl, tmp_path / "out", "p", "p", "p", material="PLA"
    )
    assert (
        "--clone-objects" not in fake_orca["cmd"] and "--orient" not in fake_orca["cmd"]
    )


async def test_too_big_is_refused_before_slicing(vendor, fake_orca, tmp_path):
    stl = _binary_stl(tmp_path / "big.stl", 320, 320, 12)
    with pytest.raises(ValueError, match="does not fit"):
        await slicer.slice_stl(
            _cfg(vendor), stl, tmp_path / "out", "p", "p", "p", material="PLA"
        )
    assert "cmd" not in fake_orca


async def test_gcode_and_log_problems_become_blockers(vendor, fake_orca, tmp_path):
    fake_orca["body"] = "G1 X320 Y10 E1\n"
    fake_orca["warn"] = True
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    res = await slicer.slice_stl(
        _cfg(vendor), stl, tmp_path / "out", "p", "p", "p", material="ABS"
    )
    text = " | ".join(res.blockers)
    assert "outside the bed" in text and "nozzle 210" in text and "Empty layer" in text


async def test_park_still_applies(vendor, fake_orca, tmp_path):
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    res = await slicer.slice_stl(
        _cfg(vendor),
        stl,
        tmp_path / "out",
        "p",
        "p",
        "p",
        park=Pose(y=200, z=100),
        material="PLA",
    )
    gcode = res.gcode.read_text()
    assert "G1 Y200.00" in gcode and "\nM84" not in gcode


async def test_identical_slices_come_from_the_cache(vendor, fake_orca, tmp_path):
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    cache = tmp_path / "cache"

    async def go(out, **kw):
        return await slicer.slice_stl(
            _cfg(vendor),
            stl,
            tmp_path / out,
            "p",
            "p",
            "p",
            material="PLA",
            cache_dir=cache,
            **kw,
        )

    first = await go("a")
    again = await go("b")
    assert fake_orca["runs"] == 1 and not first.cached and again.cached
    assert again.gcode.read_text() == first.gcode.read_text()
    assert (
        again.gcode.parent == tmp_path / "b" and again.est_seconds == first.est_seconds
    )

    hot = await go("c", filament_overrides={"nozzle_temperature": ["215"]})
    assert fake_orca["runs"] == 2 and not hot.cached
    assert fake_orca["filament"]["nozzle_temperature"] == ["215"]
    await go("d", copies=2)
    assert fake_orca["runs"] == 3


async def test_changed_geometry_misses_the_cache(vendor, fake_orca, tmp_path):
    cache = tmp_path / "cache"
    for i, dims in enumerate(((40, 40, 12), (41, 40, 12))):
        stl = _binary_stl(tmp_path / "m.stl", *dims)
        await slicer.slice_stl(
            _cfg(vendor),
            stl,
            tmp_path / f"o{i}",
            "p",
            "p",
            "p",
            material="PLA",
            cache_dir=cache,
        )
    assert fake_orca["runs"] == 2


async def test_project_mode_slices_the_plate_and_blocks_a_held_over_setting(
    vendor, fake_orca, tmp_path
):
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    project = tmp_path / "m.project.3mf"
    project.write_bytes(b"x")
    fake_orca["body"] = "G1 X10 Y10 E1\n; print-warden stripped file_start_gcode\n"
    res = await slicer.slice_stl(
        _cfg(vendor),
        stl,
        tmp_path / "out",
        "p",
        "p",
        "p",
        material="PLA",
        project=project,
        plate=2,
    )
    cmd = fake_orca["cmd"]
    assert cmd[cmd.index("--slice") + 1] == "2"
    assert cmd[-1] == str(project)
    assert res.blockers == []
    assert any("file_start_gcode" in w for w in res.warnings)
    fake_orca["body"] = "G1 X10 Y10 E1\n; print-warden stripped machine_end_gcode\n"
    res = await slicer.slice_stl(
        _cfg(vendor),
        stl,
        tmp_path / "out2",
        "p",
        "p",
        "p",
        material="PLA",
        project=project,
    )
    assert any("machine_end_gcode reached the gcode" in b for b in res.blockers)
    with pytest.raises(ValueError, match="geometry"):
        await slicer.slice_stl(
            _cfg(vendor),
            stl,
            tmp_path / "out3",
            "p",
            "p",
            "p",
            project=project,
            copies=2,
        )


async def test_a_user_printer_slices_as_its_bundled_parent(fake_orca, tmp_path):
    def put(d, kind, preset):
        (tmp_path / d / kind).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / kind / f"{preset['name']}.json").write_text(json.dumps(preset))

    put("v", "machine", {
        "name": "Vendor P", "from": "system", "machine_end_gcode": "M84",
        "printable_area": ["0x0", "300x0", "300x300", "0x300"], "printable_height": "300",
    })  # fmt: skip
    put(
        "v",
        "process",
        {"name": "q", "from": "system", "compatible_printers": ["Vendor P"]},
    )
    put("v", "filament", {"name": "f", "from": "system"})
    put("user", "machine", {"name": "Mine", "from": "User", "inherits": "Vendor P"})
    put("user", "process", {
        "name": "my q", "from": "User", "inherits": "q", "compatible_printers": ["Mine"],
    })  # fmt: skip
    cfg = SlicerConfig(
        orca="orca", vendor_dir=str(tmp_path / "v"), user_dir=str(tmp_path / "user")
    )
    stl = _binary_stl(tmp_path / "m.stl", 40, 40, 12)
    await slicer.slice_stl(
        cfg, stl, tmp_path / "out", "Mine", "my q", "f", material="PLA"
    )
    assert fake_orca["machine"]["name"] == "Mine"
    assert fake_orca["machine"]["inherits"] == "Vendor P"
    assert fake_orca["machine"]["from"] == "User"
    assert fake_orca["process"]["compatible_printers"] == ["Mine", "Vendor P"]
