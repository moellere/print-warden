import httpx
import pytest
import yaml

from print_warden import add_printer, config
from tests.test_profiles import SETTINGS, lib  # noqa: F401

BASE = "http://new.local:7125"


def _moonraker(webcams=None, devices=None, klippy="ready"):
    def handler(req: httpx.Request) -> httpx.Response:
        if "snap" in str(req.url):
            return httpx.Response(
                200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
            )
        if req.url.host != "new.local":
            return httpx.Response(404)
        path = req.url.path
        if path == "/server/info":
            return httpx.Response(200, json={"result": {"klippy_state": klippy}})
        if path == "/printer/objects/query":
            return httpx.Response(
                200, json={"result": {"status": {"configfile": {"settings": SETTINGS}}}}
            )
        if path == "/server/webcams/list":
            return httpx.Response(200, json={"result": {"webcams": webcams or []}})
        if path == "/machine/device_power/devices":
            if devices is None:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"result": {"devices": [{"device": d} for d in devices]}}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def cfg_file(lib, tmp_path):  # noqa: F811
    _, user = lib
    path = tmp_path / "printers.yaml"
    path.write_text(
        "# my printers\n"
        "printers:\n"
        "  old:   # the first one\n"
        "    moonraker: http://old.local:7125\n"
        "\n"
        "slicer:\n"
        f"  vendor_dir: {tmp_path / 'profiles' / 'B'}\n"
        f"  user_dir: {user}\n"
        "storage:\n"
        f"  dir: {tmp_path / 'data'}\n"
    )
    return path


def test_cameras_from_moonraker_webcams():
    cams = add_printer.cameras_from(
        [
            {"name": "webcam", "snapshot_url": "/webcam/?action=snapshot"},
            {"name": "Nozzle Cam", "snapshot_url": "http://cam.local/snap.jpg"},
            {"name": "empty", "snapshot_url": ""},
        ],
        BASE,
    )
    assert cams == {
        "bed": "http://new.local/webcam/?action=snapshot",
        "nozzle_cam": "http://cam.local/snap.jpg",
    }


def test_insert_keeps_comments_and_refuses_a_duplicate():
    text = "# top\nprinters:\n  a:\n    moonraker: http://a\n# tail\n"
    out = add_printer.insert(text, "b", {"moonraker": "http://b"})
    assert out.startswith("# top\nprinters:\n  b:\n    moonraker: http://b\n  a:\n")
    assert out.endswith("# tail\n")
    assert set(yaml.safe_load(out)["printers"]) == {"a", "b"}
    with pytest.raises(ValueError, match="already"):
        add_printer.insert(out, "a", {})
    assert yaml.safe_load(add_printer.insert("printers: {}\n", "b", {"x": 1})) == {
        "printers": {"b": {"x": 1}}
    }
    assert yaml.safe_load(add_printer.insert("", "b", {"x": 1})) == {
        "printers": {"b": {"x": 1}}
    }


def test_add_printer_writes_profile_entry_and_checks(cfg_file, tmp_path, capsys):
    out = tmp_path / "data" / "printers.yaml"
    client = _moonraker(
        webcams=[{"name": "webcam", "snapshot_url": "/webcam/?action=snapshot"}],
        devices=["printer"],
    )
    rc = add_printer.main(
        ["voron", "--moonraker", BASE, "--location", "garage", "--config", str(cfg_file),
         "--write", str(out)],
        client,
    )  # fmt: skip
    text = capsys.readouterr().out
    assert rc == 0, text
    assert (tmp_path / "user" / "machine" / "voron (Klipper).json").exists()
    written = out.read_text()
    assert written.startswith("# my printers\nprinters:\n  voron:\n")
    assert "  old:   # the first one\n" in written
    cfg = config.load(str(out))
    p = cfg.printers["voron"]
    assert (p.location, p.moonraker, p.power_device) == ("garage", BASE, "printer")
    assert p.cameras == {"bed": "http://new.local/webcam/?action=snapshot"}
    assert p.machine == "voron (Klipper)"
    assert cfg_file.read_text().count("voron") == 0
    assert "ok    voron moonraker" in text
    assert "--printer voron --rows" in text


def test_print_only_without_write_and_named_cameras(cfg_file, capsys):
    before = cfg_file.read_text()
    rc = add_printer.main(
        ["v2", "--moonraker", BASE, "--config", str(cfg_file), "--camera", "nozzle=http://c/snap.jpg"],
        _moonraker(devices=["a", "b"], klippy="shutdown"),
    )  # fmt: skip
    text = capsys.readouterr().out
    assert rc == 0, text
    assert cfg_file.read_text() == before
    assert "note: Klipper is 'shutdown'" in text
    assert "nozzle: http://c/snap.jpg" in text
    assert "power_device" not in text


def test_failures(cfg_file, capsys):
    assert add_printer.main(["bad name", "--moonraker", BASE], _moonraker()) == 2
    assert (
        add_printer.main(
            ["old", "--moonraker", BASE, "--config", str(cfg_file)], _moonraker()
        )
        == 1
    )
    assert "already in the config" in capsys.readouterr().out
    rc = add_printer.main(
        ["x", "--moonraker", "http://gone.local:7125", "--config", str(cfg_file)],
        _moonraker(),
    )
    assert rc == 1
    assert "fail:" in capsys.readouterr().out
