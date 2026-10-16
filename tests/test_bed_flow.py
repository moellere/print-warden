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
