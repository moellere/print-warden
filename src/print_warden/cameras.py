"""Snapshot fetching. Any HTTP URL that returns a JPEG (crowsnest, Frigate latest.jpg, Thingino)."""

from __future__ import annotations

import io
import time
from pathlib import Path

import httpx
from PIL import Image


async def fetch(
    url: str, rotate: int = 0, timeout: float = 8.0, flip_v: bool = False
) -> bytes:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        data = r.content
    if rotate or flip_v:
        im = Image.open(io.BytesIO(data))
        if flip_v:
            im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if rotate:
            im = im.rotate(-rotate, expand=True)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        data = buf.getvalue()
    return data


def save(
    root: Path, printer: str, camera: str, data: bytes, subdir: str = "snapshots"
) -> Path:
    d = root / subdir / printer
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}_{camera}.jpg"
    p.write_bytes(data)
    return p
