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
        out_dir = cmd[cmd.index("--outputdir") + 1]
        name = cmd[cmd.index("--export-3mf") + 1]
        machine = json.loads(
            Path(cmd[cmd.index("--load-settings") + 1].split(";")[0]).read_text()
        )
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
