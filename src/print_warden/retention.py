"""Storage retention: snapshots, timelapse frames and slice-cache entries untouched for keep_days are deleted (a cache
hit refreshes its entry). Rendered timelapse videos, job artifacts and uploaded models are kept."""

from __future__ import annotations

import time
from pathlib import Path


def prune_files(root: Path, keep_days: int, now: float | None = None) -> int:
    cutoff = (now or time.time()) - keep_days * 86400
    removed = 0
    for sub, pattern in (
        ("snapshots", "*"),
        ("timelapse", "*.jpg"),
        ("slice-cache", "*"),
    ):
        base = root / sub
        if not base.is_dir():
            continue
        for f in base.rglob(pattern):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        for d in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return removed
