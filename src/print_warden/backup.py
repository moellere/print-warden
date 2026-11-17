"""`print-warden backup` / `print-warden restore`: the warden's state as one archive.

    print-warden backup [--config printers.yaml] [--out print-warden-<time>.tar.gz]
    print-warden restore ARCHIVE [--config printers.yaml] [--force]

The archive holds consistent copies of the three databases (jobs, events, bed checks; taken with SQLite's online
backup, so the warden may keep running), the warden's own slicer profiles, the empty-bed reference images,
`validation.json` and a `manifest.json`
(version, time, files). Snapshots, timelapses and job artifacts are not in it: they are re-creatable or pruned anyway,
and models can be uploaded again. Secrets never are: they live in the environment, not in storage.

Restore refuses while a warden answers on this config's port (stop it first, or `--force`). Files it replaces are
kept beside the originals as `<name>.before-restore-<time>`. Only the names a backup writes are extracted.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

import httpx

from . import __version__, config

DATABASES = ("jobs.db", "events.db", "bed.db")
FILES = ("validation.json",)
DIRS = ("profiles", "reference")


def _db_copy(src: Path) -> bytes:
    """A consistent copy of a live SQLite database."""
    tmp = src.with_name(f".{src.name}.backup")
    try:
        with sqlite3.connect(src) as a, sqlite3.connect(tmp) as b:
            a.backup(b)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def backup(storage: Path, out: Path) -> dict[str, object]:
    """Write the archive; returns its manifest."""
    names: list[str] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for db in DATABASES:
            if (storage / db).is_file():
                _add(tar, db, _db_copy(storage / db))
                names.append(db)
        for f in FILES:
            if (storage / f).is_file():
                _add(tar, f, (storage / f).read_bytes())
                names.append(f)
        for d in DIRS:
            for p in sorted((storage / d).rglob("*")) if (storage / d).is_dir() else []:
                if p.is_file():
                    rel = p.relative_to(storage).as_posix()
                    _add(tar, rel, p.read_bytes())
                    names.append(rel)
        manifest = {"version": __version__, "created": time.time(), "files": names}
        _add(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
    return manifest


def _allowed(name: str) -> bool:
    parts = Path(name).parts
    if not parts or name.startswith("/") or ".." in parts:
        return False
    return (
        name in DATABASES
        or name in FILES
        or name == "manifest.json"
        or parts[0] in DIRS
    )


def restore(storage: Path, archive: Path) -> list[str]:
    """Put an archive's files back; returns what was restored."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    restored = []
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not (m.isfile() and _allowed(m.name))]
        if bad:
            raise ValueError(
                f"not a print-warden backup (unexpected entries: {', '.join(bad[:5])})"
            )
        if "manifest.json" not in {m.name for m in members}:
            raise ValueError("not a print-warden backup (no manifest.json)")
        storage.mkdir(parents=True, exist_ok=True)
        for m in members:
            if m.name == "manifest.json":
                continue
            dst = storage / m.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.move(dst, dst.with_name(f"{dst.name}.before-restore-{stamp}"))
            f = tar.extractfile(m)
            assert f is not None
            dst.write_bytes(f.read())
            restored.append(m.name)
    return restored


def _running(port: int) -> bool:
    try:
        return (
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200
        )
    except httpx.HTTPError:
        return False


def main(verb: str, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog=f"print-warden {verb}", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "--config", default=os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    )
    if verb == "backup":
        ap.add_argument(
            "--out", help="the archive (default print-warden-<time>.tar.gz)"
        )
    else:
        ap.add_argument("archive")
        ap.add_argument(
            "--force", action="store_true", help="restore even while a warden answers"
        )
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if verb == "backup":
        out = Path(args.out or f"print-warden-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        manifest = backup(cfg.storage_dir, out)
        print(f"{out}: {len(manifest['files'])} file(s) from {cfg.storage_dir}")  # type: ignore[arg-type]
        return 0
    if _running(cfg.port) and not args.force:
        print(f"a warden answers on port {cfg.port}: stop it first (or --force)")
        return 1
    try:
        restored = restore(cfg.storage_dir, Path(args.archive))
    except (OSError, ValueError, tarfile.TarError) as e:
        print(f"restore failed: {e}")
        return 1
    print(
        f"restored {len(restored)} file(s) into {cfg.storage_dir}; replaced files kept as *.before-restore-*"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
