import io

from PIL import Image, ImageDraw

from print_warden.bedcheck import BedStore, Check, score


def _jpg(brightness=120, box=None, size=(640, 480)):
    im = Image.new("RGB", size, (brightness, brightness, brightness))
    d = ImageDraw.Draw(im)
    for x in range(0, size[0], 80):
        d.line([(x, 0), (x, size[1])], fill=(brightness - 30,) * 3, width=3)
    if box:
        d.rectangle(box, fill=(20, 200, 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_identical_bed_scores_zero():
    assert score(_jpg(), _jpg()) < 0.005


def test_lighting_change_is_tolerated():
    assert score(_jpg(120), _jpg(150)) < 0.02


def test_object_on_bed_is_seen():
    assert score(_jpg(), _jpg(box=(250, 180, 360, 280))) > 0.03


def test_object_outside_region_is_ignored():
    obj = _jpg(box=(10, 10, 100, 100))
    assert score(_jpg(), obj) > 0.02
    assert score(_jpg(), obj, region=(0.25, 0.25, 1.0, 1.0)) < 0.005


def test_ledger(tmp_path):
    store = BedStore(tmp_path / "bed.db")
    assert store.state("p")["state"] == "unknown"
    store.set_state("p", "occupied", "print_complete")
    assert store.state("p")["state"] == "occupied"
    store.record("p", Check(1.0, "occupied", 0.2, False, None))
    store.record("p", Check(2.0, None, 0.0, True, None))
    store.record("p", Check(3.0, "clear", 0.05, False, None))
    hist = store.labelled("p", 10)
    assert [c.ts for c in hist] == [3.0, 1.0]
    assert [c.agrees for c in hist] == [False, True]
