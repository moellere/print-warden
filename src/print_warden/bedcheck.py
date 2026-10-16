"""Bed-clear check. Deterministic image comparison against an operator-attested empty-bed reference, plus the bed
ledger: the bed's state (clear / occupied) and every check taken while that state was known, so the vision verdict
earns or loses the trust auto_start relies on. No model is consulted."""

from __future__ import annotations

import io
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

SIZE = (160, 120)
PIXEL_DELTA = 28


def _prep(data: bytes, region: tuple[float, float, float, float]) -> Image.Image:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = region
    im = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    return im.resize(SIZE).filter(ImageFilter.GaussianBlur(1))


def _mean(band: Image.Image) -> float:
    hist = band.histogram()
    return sum(i * n for i, n in enumerate(hist)) / max(1, sum(hist))


def score(
    reference: bytes,
    current: bytes,
    region: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> float:
    """Fraction of the bed region that differs from the reference, per colour channel after matching each channel's
    brightness, so a part the same grey value as the bed but a different colour still counts."""
    ref, cur = _prep(reference, region), _prep(current, region)
    bands = []
    for r, c in zip(ref.split(), cur.split(), strict=True):
        gain = _mean(r) / max(1.0, _mean(c))
        bands.append(
            ImageChops.difference(r, c.point(lambda v, g=gain: min(255, round(v * g))))
        )
    diff = bands[0]
    for b in bands[1:]:
        diff = ImageChops.lighter(diff, b)
    changed = diff.point(lambda v: 255 if v > PIXEL_DELTA else 0).histogram()[255]
    return changed / (SIZE[0] * SIZE[1])


@dataclass(frozen=True)
class Check:
    ts: float
    label: str | None
    score: float
    clear: bool
    snapshot: str | None

    @property
    def agrees(self) -> bool | None:
        return None if self.label is None else (self.label == "clear") == self.clear


class BedStore:
    """Bed state per printer and the labelled check history."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "create table if not exists bed (printer text primary key, state text, since real, note text)"
        )
        self._db.execute(
            "create table if not exists checks (id integer primary key, printer text, ts real, label text,"
            " score real, clear integer, snapshot text)"
        )
        self._db.commit()

    def state(self, printer: str) -> dict[str, Any]:
        r = self._db.execute(
            "select state, since, note from bed where printer=?", (printer,)
        ).fetchone()
        if not r:
            return {"state": "unknown", "since": None, "note": None}
        return {"state": r[0], "since": r[1], "note": r[2]}

    def set_state(self, printer: str, state: str, note: str = "") -> None:
        if state not in ("clear", "occupied"):
            raise ValueError(state)
        self._db.execute(
            "insert into bed (printer, state, since, note) values (?,?,?,?) on conflict(printer) do update set"
            " state=excluded.state, since=excluded.since, note=excluded.note",
            (printer, state, time.time(), note),
        )
        self._db.commit()

    def record(self, printer: str, check: Check) -> None:
        self._db.execute(
            "insert into checks (printer, ts, label, score, clear, snapshot) values (?,?,?,?,?,?)",
            (
                printer,
                check.ts,
                check.label,
                check.score,
                int(check.clear),
                check.snapshot,
            ),
        )
        self._db.commit()

    def labelled(self, printer: str, limit: int) -> list[Check]:
        rows = self._db.execute(
            "select ts, label, score, clear, snapshot from checks where printer=? and label is not null"
            " order by ts desc limit ?",
            (printer, limit),
        )
        return [Check(ts, label, sc, bool(c), snap) for ts, label, sc, c, snap in rows]
