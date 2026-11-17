import json
import sqlite3
import tarfile

import pytest

from print_warden import backup
from print_warden.bedcheck import BedStore
from print_warden.events import Event, EventStore
from print_warden.jobs import JobStore


def _storage(root):
    jobs = JobStore(root / "jobs.db")
    job = jobs.create(printer="p", name="cube", material="PLA")
    jobs.kv_set("loaded_material:p", "PLA")
    EventStore(root / "events.db").add(
        Event("p", "print_started", "info", "Started", {})
    )
    BedStore(root / "bed.db").set_state("p", "occupied", "test")
    (root / "validation.json").write_text('{"rows": {}}')
    (root / "profiles" / "machine").mkdir(parents=True)
    (root / "profiles" / "machine" / "mine.json").write_text('{"name": "mine"}')
    (root / "reference").mkdir()
    (root / "reference" / "p.jpg").write_bytes(b"jpg")
    return job


def test_backup_and_restore_round_trip(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.mkdir()
    job = _storage(src)
    archive = tmp_path / "w.tar.gz"
    manifest = backup.backup(src, archive)
    assert set(manifest["files"]) == {
        "jobs.db", "events.db", "bed.db", "validation.json", "profiles/machine/mine.json", "reference/p.jpg"
    }  # fmt: skip
    dst.mkdir()
    (dst / "jobs.db").write_bytes(b"old")
    restored = backup.restore(dst, archive)
    assert "jobs.db" in restored and "manifest.json" not in restored
    assert JobStore(dst / "jobs.db").get(job["id"])["name"] == "cube"
    assert JobStore(dst / "jobs.db").kv_get("loaded_material:p") == "PLA"
    assert BedStore(dst / "bed.db").state("p")["state"] == "occupied"
    assert (
        dst / "profiles" / "machine" / "mine.json"
    ).read_text() == '{"name": "mine"}'
    assert [p.read_bytes() for p in dst.glob("jobs.db.before-restore-*")] == [b"old"]


def test_restore_refuses_what_a_backup_would_not_write(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for name in ("manifest.json", "../escape.txt"):
            p = tmp_path / "f"
            p.write_text("{}")
            tar.add(p, arcname=name)
    with pytest.raises(ValueError, match="unexpected entries"):
        backup.restore(tmp_path / "s", bad)
    assert not (tmp_path / "escape.txt").exists()
    plain = tmp_path / "plain.tar.gz"
    with tarfile.open(plain, "w:gz") as tar:
        tar.add(tmp_path / "f", arcname="jobs.db")
    with pytest.raises(ValueError, match="no manifest"):
        backup.restore(tmp_path / "s", plain)


def test_cli_backup_and_restore(tmp_path, monkeypatch, capsys):
    store = tmp_path / "data"
    store.mkdir()
    _storage(store)
    cfg = tmp_path / "printers.yaml"
    cfg.write_text(f"printers: {{}}\nstorage: {{dir: {store}}}\nserver: {{port: 9}}\n")
    out = tmp_path / "b.tar.gz"
    assert backup.main("backup", ["--config", str(cfg), "--out", str(out)]) == 0
    with tarfile.open(out) as tar:
        assert json.loads(tar.extractfile("manifest.json").read())["files"]
    assert backup.main("restore", ["--config", str(cfg), str(out)]) == 0
    monkeypatch.setattr(backup, "_running", lambda port: True)
    assert backup.main("restore", ["--config", str(cfg), str(out)]) == 1
    assert "stop it first" in capsys.readouterr().out
    assert backup.main("restore", ["--config", str(cfg), str(out), "--force"]) == 0


def test_a_live_database_is_copied_consistently(tmp_path):
    jobs = JobStore(tmp_path / "jobs.db")
    jobs.create(printer="p", name="x", material="PLA")
    data = backup._db_copy(tmp_path / "jobs.db")
    (tmp_path / "copy.db").write_bytes(data)
    assert sqlite3.connect(tmp_path / "copy.db").execute(
        "select count(*) from jobs"
    ).fetchone() == (1,)
