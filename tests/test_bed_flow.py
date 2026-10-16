import dataclasses
import io
import os

import pytest
from PIL import Image, ImageDraw

from print_warden import config
from print_warden import watcher as watcher_mod
from print_warden.bedcheck import BedStore
from print_warden.events import Event, EventStore
from print_warden.watcher import PrinterWatcher


def _jpg(box=None):
    im = Image.new("RGB", (320, 240), (120, 120, 120))
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=(20, 200, 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def frames(monkeypatch):
    feed = {"img": _jpg()}

    async def fetch(url, rotate=0, timeout=8.0, flip_v=False):
        return feed["img"]

    monkeypatch.setattr(watcher_mod.cameras, "fetch", fetch)
    monkeypatch.setattr(watcher_mod, "BED_SETTLE_S", 0)
    return feed


@pytest.fixture
def w(tmp_path):
    p = config.Printer(
        name="p",
        moonraker="http://127.0.0.1:9",
        cameras={"bed": "http://cam"},
        bed_check=config.BedCheck(min_agreements=2),
    )
    cfg = config.Config(
        printers={"p": p},
        ha=None,
        obico_url=None,
        obico_webhook_secret=None,
        storage_dir=tmp_path,
        keep_days=14,
        host="h",
        port=1,
        public_url="http://w",
    )
    return PrinterWatcher(
        cfg,
        p,
        EventStore(tmp_path / "e.db"),
        None,
        None,
        None,
        BedStore(tmp_path / "bed.db"),
    )


async def test_first_clear_captures_reference_then_grades(w, frames):
    out = await w.mark_clear("phone tap")
    assert out["reference"] == "captured" and w.reference_path.exists()
    assert w.bed.state("p")["state"] == "clear"

    out = await w.mark_clear("phone tap")
    assert out["check"]["clear"] and out["check"]["agrees"]

    frames["img"] = _jpg(box=(100, 80, 200, 160))
    out = await w.mark_clear("phone tap")
    assert not out["check"]["clear"] and out["check"]["agrees"] is False
    assert [c.agrees for c in w.bed_history()] == [False, True]


async def test_print_lifecycle_marks_bed_occupied_and_labels_a_check(w, frames):
    await w.mark_clear("phone tap")
    await w.handle(
        Event("p", "print_started", "info", "Started", {"filename": "a.gcode"})
    )
    assert w.bed.state("p")["state"] == "occupied"
    frames["img"] = _jpg(box=(100, 80, 200, 160))
    await w.handle(
        Event("p", "print_complete", "info", "Complete", {"filename": "a.gcode"})
    )
    await __import__("asyncio").gather(*w._tasks)
    hist = w.bed_history()
    assert hist[0].label == "occupied" and not hist[0].clear and hist[0].agrees


async def test_new_reference_restarts_the_record(w, frames):
    await w.mark_clear("tap")
    await w.mark_clear("tap")
    assert len(w.bed_history()) == 1
    old = w.reference_path.stat().st_mtime
    await w.capture_reference()
    os.utime(w.reference_path, (old + 10, old + 10))
    assert w.bed_history() == []


async def test_cannot_clear_while_printing(w, frames):
    w.mr.state = {"print_stats": {"state": "printing"}}
    with pytest.raises(ValueError, match="printing"):
        await w.mark_clear("tap")


async def test_no_check_without_reference(w, frames):
    assert await w.check_bed() is None
    w.p = dataclasses.replace(w.p, bed_check=None)
    with pytest.raises(ValueError):
        await w.capture_reference()


@pytest.fixture
def posed(w):
    w.p = dataclasses.replace(
        w.p,
        bed_check=dataclasses.replace(w.p.bed_check, pose=config.Pose(y=220, z=150)),
    )
    return w


async def test_check_and_reference_need_the_pose(posed, frames):
    w = posed
    w.mr.state = {"toolhead": {"homed_axes": "", "position": [0, 30, 160, 0]}}
    with pytest.raises(ValueError, match="bed-check pose"):
        await w.capture_reference()
    w.mr.state = {"toolhead": {"homed_axes": "xyz", "position": [0, 220, 160, 0]}}
    await w.capture_reference()
    assert (await w.check_bed()).clear
    w.mr.state = {"toolhead": {"homed_axes": "", "position": [0, 220, 160, 0]}}
    assert await w.check_bed() is None
    assert "steppers released" in w.check_skipped
    out = await w.mark_clear("tap")
    assert "steppers released" in out["check"]["skipped"]


async def test_rehome_y_moves_only_y_when_idle_and_z_is_high(
    posed, frames, monkeypatch
):
    w = posed
    w.p = dataclasses.replace(
        w.p, bed_check=dataclasses.replace(w.p.bed_check, rehome_y=True)
    )
    scripts = []

    async def gcode_script(base, script, timeout=120):
        scripts.append(script)
        w.mr.state["toolhead"] = {"homed_axes": "y", "position": [0, 220, 160, 0]}

    monkeypatch.setattr(watcher_mod.moonraker, "gcode_script", gcode_script)
    monkeypatch.setattr(watcher_mod.asyncio, "sleep", _no_sleep)
    w.mr.state = {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "complete"},
        "toolhead": {"homed_axes": "", "position": [0, 37, 160, 0]},
    }
    ok, why = await w.ensure_pose()
    assert ok, why
    assert scripts == ["G28 Y\nG90\nG1 Y220.00 F3000\nM400"]
    assert w.store.query(min_severity="warning")[0]["kind"] == "bed_rehomed"

    scripts.clear()
    assert (await w.ensure_pose())[0] and scripts == []


async def test_rehome_refused_when_z_is_low_or_printer_busy(posed, frames, monkeypatch):
    w = posed
    w.p = dataclasses.replace(
        w.p, bed_check=dataclasses.replace(w.p.bed_check, rehome_y=True)
    )

    async def boom(*a, **k):
        raise AssertionError("must not move")

    monkeypatch.setattr(watcher_mod.moonraker, "gcode_script", boom)
    w.mr.state = {
        "webhooks": {"state": "ready"},
        "print_stats": {"state": "standby"},
        "toolhead": {"homed_axes": "", "position": [0, 37, 20, 0]},
    }
    ok, why = await w.ensure_pose()
    assert not ok and "last Z is below" in why
    w.mr.state["print_stats"]["state"] = "printing"
    w.mr.state["toolhead"]["position"] = [0, 37, 160, 0]
    ok, why = await w.ensure_pose()
    assert not ok and "printer is printing" in why


async def _no_sleep(_s):
    return None
