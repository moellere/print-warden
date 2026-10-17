import io
import json
import zipfile

import pytest

from print_warden import profiles, slicer
from print_warden.config import SlicerConfig


def _put(root, vendor, kind, preset, file=None):
    d = root / vendor / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file or preset['name']}.json").write_text(json.dumps(preset))


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "profiles"
    sys_ = {"from": "system"}
    _put(
        root,
        "A",
        "machine",
        {
            **sys_,
            "name": "fdm_machine_common",
            "instantiation": "false",
            "gcode_flavor": "klipper",
        },
    )
    _put(root, "A", "machine", {**sys_, "name": "A One 0.4", "inherits": "fdm_machine_common", "instantiation": "true",
                                "printable_area": ["0x0", "200x0", "200x200", "0x200"], "printable_height": "200"},
         file="a1")  # fmt: skip
    _put(
        root,
        "B",
        "machine",
        {
            **sys_,
            "name": "fdm_machine_common",
            "instantiation": "false",
            "gcode_flavor": "marlin",
        },
    )
    _put(
        root,
        "B",
        "machine",
        {
            **sys_,
            "name": "B Two",
            "inherits": "fdm_machine_common",
            "instantiation": "true",
        },
    )
    _put(root, "Custom", "machine", {**sys_, "name": "MyKlipper 0.4 nozzle", "instantiation": "true",
                                     "nozzle_diameter": ["0.4"]})  # fmt: skip
    _put(root, "OrcaFilamentLibrary", "filament", {**sys_, "name": "fdm_filament_pla", "instantiation": "false",
                                                   "filament_type": ["PLA"], "nozzle_temperature": ["210"]})  # fmt: skip
    _put(root, "OrcaFilamentLibrary", "filament", {**sys_, "name": "Generic PLA @System", "inherits": "fdm_filament_pla",
                                                   "instantiation": "true", "compatible_printers": []})  # fmt: skip
    _put(root, "OrcaFilamentLibrary", "filament", {**sys_, "name": "Generic PETG @System", "instantiation": "true",
                                                   "filament_type": ["PETG"]})  # fmt: skip
    _put(root, "A", "filament", {**sys_, "name": "A Generic PLA", "inherits": "fdm_filament_pla", "instantiation": "true",
                                 "compatible_printers": ["A One 0.4"]})  # fmt: skip
    _put(root, "A", "process", {**sys_, "name": "0.20mm Standard @A", "instantiation": "true", "layer_height": "0.2",
                                "compatible_printers": ["A One 0.4"]})  # fmt: skip
    _put(root, "A", "process", {**sys_, "name": "0.24mm Draft @A", "inherits": "0.20mm Standard @A",
                                "instantiation": "true", "layer_height": "0.24"})  # fmt: skip
    _put(root, "A", "process", {**sys_, "name": "0.28mm Standard @A", "inherits": "0.20mm Standard @A",
                                "instantiation": "true", "layer_height": "0.28"})  # fmt: skip
    _put(root, "B", "process", {**sys_, "name": "0.20mm Standard @B", "instantiation": "true",
                                "compatible_printers": ["B Two"]})  # fmt: skip
    cfg = SlicerConfig(vendor_dir=str(root / "B"), user_dir=str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    return slicer.search_dirs(cfg), tmp_path / "user"


def test_presets_resolve_by_name_with_parents_from_their_own_vendor(lib):
    dirs, _ = lib
    assert [d.name for d in dirs][:3] == ["user", "B", "OrcaFilamentLibrary"]
    a = slicer.flatten(dirs, "machine", "A One 0.4")
    assert a["gcode_flavor"] == "klipper"
    assert slicer.flatten(dirs, "machine", "B Two")["gcode_flavor"] == "marlin"
    assert slicer.flatten(dirs, "filament", "A Generic PLA")["nozzle_temperature"] == [
        "210"
    ]
    with pytest.raises(FileNotFoundError):
        slicer.flatten(dirs, "machine", "nope")


def test_vendors_printers_and_the_block(lib):
    dirs, _ = lib
    assert profiles.vendors(dirs) == ["A", "B", "Custom"]
    assert profiles.printers(dirs, "A") == ["A One 0.4"]
    b = profiles.block(dirs, "A One 0.4")
    assert b["processes"] == {
        "draft": "0.24mm Draft @A",
        "standard": "0.20mm Standard @A",
        "standard_0.28": "0.28mm Standard @A",
    }
    assert b["filaments"] == {"PLA": "A Generic PLA", "PETG": "Generic PETG @System"}


def test_a_user_preset_names_its_bundled_parent(lib):
    dirs, user = lib
    _put(user.parent, "user", "machine", {"name": "Mine", "from": "User", "inherits": "A One 0.4",
                                          "printable_height": "150"})  # fmt: skip
    m = slicer.flatten(dirs, "machine", "Mine")
    assert (m["inherits"], m["from"], m["printable_height"]) == (
        "A One 0.4",
        "User",
        "150",
    )
    assert profiles.block(dirs, "Mine")["processes"]["standard"] == "0.20mm Standard @A"
    _put(user.parent, "user", "machine", {"name": "Loose", "from": "User"})
    with pytest.raises(ValueError, match="no OrcaSlicer system preset"):
        slicer.flatten(dirs, "machine", "Loose")


def test_import_presets_and_bundles(lib, tmp_path):
    dirs, user = lib
    single = tmp_path / "Mine.json"
    single.write_text(
        json.dumps(
            {"type": "machine", "name": "Mine", "inherits": "A One 0.4", "from": "User"}
        )
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "bundle_structure.json",
            json.dumps({"bundle_type": "printer config bundle"}),
        )
        z.writestr(
            "filament/My PLA.json",
            json.dumps(
                {
                    "name": "My PLA",
                    "inherits": "Generic PLA @System",
                    "compatible_printers": ["Mine"],
                }
            ),
        )
        z.writestr(
            "process/x.json",
            json.dumps(
                {
                    "name": "0.2 mine",
                    "inherits": "0.20mm Standard @A",
                    "post_process": ["/bin/evil"],
                }
            ),
        )
        z.writestr(
            "filament/orphan.json",
            json.dumps({"name": "Orphan", "inherits": "Bambu-only parent"}),
        )
        z.writestr("readme.txt", "hi")
    bundle = tmp_path / "mine.orca_printer"
    bundle.write_bytes(buf.getvalue())  # fmt: skip
    done, problems = profiles.import_presets([single, bundle], user, dirs)
    assert sorted(done) == [
        ("filament", "My PLA"),
        ("machine", "Mine"),
        ("process", "0.2 mine"),
    ]
    assert any("post_process dropped" in p for p in problems)
    assert any("'Orphan' not imported" in p for p in problems)
    assert not (user / "filament" / "Orphan.json").exists()
    proc = json.loads((user / "process" / "0.2 mine.json").read_text())
    assert proc["post_process"] == [] and proc["from"] == "User"
    assert profiles.block(dirs, "Mine")["filaments"]["PLA"] == "My PLA"


def test_import_names_cannot_escape_the_profile_dir(lib, tmp_path):
    dirs, user = lib
    evil = tmp_path / "evil.json"
    evil.write_text(
        json.dumps({"type": "machine", "name": "../../x", "inherits": "A One 0.4"})
    )
    done, _ = profiles.import_presets([evil], user, dirs)
    assert done == [("machine", "../../x")]
    assert [p.parent for p in user.rglob("*.json")] == [user / "machine"]


SETTINGS = {
    "printer": {"kinematics": "corexy", "max_velocity": 300.0, "max_accel": 5000.0},
    "stepper_x": {"position_min": -5.0, "position_max": 350.0},
    "stepper_y": {"position_min": 0.0, "position_max": 345.0},
    "stepper_z": {"position_max": 330.0},
    "extruder": {"nozzle_diameter": 0.6, "filament_diameter": 1.75},
    "gcode_macro print_start": {
        "gcode": "{% set b = params.BED_TEMP %}{% set e = params.HOTEND %}G28"
    },
    "gcode_macro end_print": {"gcode": "M84"},
}


def test_a_profile_from_klipper(lib):
    dirs, _ = lib
    p = profiles.from_klipper(SETTINGS, "Voron", dirs)
    assert p["inherits"] == "MyKlipper 0.4 nozzle"
    assert p["printable_area"] == ["0x0", "350x0", "350x345", "0x345"]
    assert (p["printable_height"], p["nozzle_diameter"]) == ("330", ["0.6"])
    assert p["printer_structure"] == "corexy"
    assert p["machine_max_acceleration_x"] == ["5000", "5000"]
    assert p["machine_start_gcode"].startswith(
        "PRINT_START BED_TEMP=[bed_temperature_initial_layer_single] HOTEND=[nozzle_temperature_initial_layer]"
    )
    assert p["machine_end_gcode"] == "END_PRINT\n"
    bare = {k: v for k, v in SETTINGS.items() if not k.startswith("gcode_macro")}
    plain = profiles.from_klipper(bare, "Plain", dirs)
    assert (
        "G28" in plain["machine_start_gcode"] and "M109" in plain["machine_start_gcode"]
    )
    assert "M84" in plain["machine_end_gcode"]
    params = {**SETTINGS, "gcode_macro print_start": {"gcode": "G28"}}
    assert profiles.from_klipper(params, "N", dirs)["machine_start_gcode"].endswith(
        "PRINT_START\n"
    )
    with pytest.raises(ValueError, match="delta"):
        profiles.from_klipper(
            {**SETTINGS, "printer": {"kinematics": "delta"}}, "D", dirs
        )
    with pytest.raises(ValueError, match="position_max"):
        profiles.from_klipper({"printer": {}}, "X", dirs)
    with pytest.raises(FileNotFoundError):
        profiles.from_klipper(SETTINGS, "X", dirs, base="nope")


def test_the_cli(lib, tmp_path, monkeypatch, capsys):
    dirs, user = lib
    cfg = tmp_path / "printers.yaml"
    cfg.write_text(
        f"slicer:\n  vendor_dir: {dirs[1]}\n  user_dir: {user}\nstorage:\n  dir: {tmp_path}\n"
    )

    async def settings(url):
        assert url == "http://p:7125"
        return SETTINGS

    monkeypatch.setattr(profiles, "_settings", settings)
    assert profiles.main(["--config", str(cfg), "vendors"]) == 0
    assert capsys.readouterr().out.split() == ["A", "B", "Custom"]
    assert profiles.main(["--config", str(cfg), "printers", "nobody"]) == 1
    assert profiles.main(["--config", str(cfg), "block", "A One 0.4"]) == 0
    out = capsys.readouterr().out
    assert "machine: A One 0.4" in out and "standard: 0.20mm Standard @A" in out
    assert (
        profiles.main(["--config", str(cfg), "klipper", "Voron", "http://p:7125"]) == 0
    )
    assert (user / "machine" / "Voron.json").exists()
    assert "machine: Voron" in capsys.readouterr().out
    assert profiles.main(["--config", str(cfg), "block", "missing"]) == 1
