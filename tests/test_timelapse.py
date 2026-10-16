import shutil

import pytest
from PIL import Image

from print_warden import timelapse


async def test_too_few_frames(tmp_path):
    assert await timelapse.render(tmp_path) is None


async def test_missing_ffmpeg_is_not_fatal(tmp_path):
    for i in range(2):
        Image.new("RGB", (8, 8)).save(tmp_path / f"L{i:05d}.jpg")
    assert await timelapse.render(tmp_path, ffmpeg="/nonexistent/ffmpeg") is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_renders_mp4(tmp_path):
    for i in range(3):
        Image.new("RGB", (33, 17), (i * 80, 0, 0)).save(tmp_path / f"L{i:05d}.jpg")
    out = await timelapse.render(tmp_path)
    assert out is not None and out.stat().st_size > 0
