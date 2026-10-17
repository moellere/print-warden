import os

from print_warden.retention import prune_files


def _touch(path, age_days, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (now - age_days * 86400,) * 2)


def test_prunes_old_snapshots_and_frames_but_keeps_videos(tmp_path):
    now = 10_000_000.0
    _touch(tmp_path / "snapshots/p/old.jpg", 20, now)
    _touch(tmp_path / "snapshots/p/new.jpg", 1, now)
    _touch(tmp_path / "timelapse/p/run/L00001.jpg", 20, now)
    _touch(tmp_path / "timelapse/p/run/timelapse.mp4", 20, now)
    _touch(tmp_path / "events.db", 20, now)
    _touch(tmp_path / "snapshots/q/gone.jpg", 20, now)
    assert prune_files(tmp_path, 14, now) == 3
    assert (tmp_path / "snapshots/p/new.jpg").exists()
    assert (tmp_path / "timelapse/p/run/timelapse.mp4").exists()
    assert (tmp_path / "events.db").exists()
    assert not (tmp_path / "snapshots/q").exists()


def test_prunes_stale_slice_cache_entries(tmp_path):
    now = 10_000_000.0
    _touch(tmp_path / "slice-cache/old/slice.gcode", 20, now)
    _touch(tmp_path / "slice-cache/old/result.json", 20, now)
    _touch(tmp_path / "slice-cache/hot/result.json", 1, now)
    assert prune_files(tmp_path, 14, now) == 2
    assert not (tmp_path / "slice-cache/old").exists()
    assert (tmp_path / "slice-cache/hot/result.json").exists()
