"""Timelapse assembly: the per-layer frames of one print become an mp4 next to them."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def render(
    frames: Path, ffmpeg: str = "ffmpeg", fps: int = 30, timeout_s: float = 300
) -> Path | None:
    if len(list(frames.glob("*.jpg"))) < 2:
        return None
    out = frames / "timelapse.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-pattern_type",
        "glob",
        "-i",
        str(frames / "*.jpg"),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as e:
        log.warning("timelapse: cannot run %s: %s", ffmpeg, e)
        return None
    try:
        msg, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        log.warning("timelapse %s: ffmpeg timed out", frames)
        return None
    if proc.returncode != 0 or not out.exists():
        log.warning("timelapse %s: %s", frames, msg.decode(errors="ignore")[-500:])
        return None
    return out
