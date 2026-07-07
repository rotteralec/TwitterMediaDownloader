"""Tests for app.gifconv — the ffmpeg MP4->GIF conversion.

These need the ffmpeg/ffprobe binaries; they're skipped automatically if ffmpeg
isn't on PATH (it's required in production and in the Docker image).
"""

import asyncio
import shutil
import subprocess

import pytest

from app.gifconv import GifConvError, ffmpeg_available, mp4_to_gif

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available() or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _make_silent_mp4(path, width=480, height=270, seconds=1, rate=20):
    """Generate a silent test MP4 (-an = no audio), like an X GIF source."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size={width}x{height}:rate={rate}",
            "-pix_fmt", "yuv420p", "-an", str(path),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _dimensions(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def _frame_count(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return int(out)


def test_ffmpeg_available_returns_bool():
    assert isinstance(ffmpeg_available(), bool)


@requires_ffmpeg
def test_mp4_to_gif_produces_animated_gif(tmp_path):
    src = tmp_path / "in.mp4"
    dst = tmp_path / "out.gif"
    _make_silent_mp4(src, width=480, height=270, seconds=2, rate=20)

    asyncio.run(mp4_to_gif(str(src), str(dst), max_width=480, fps=15, timeout=60))

    assert dst.exists() and dst.stat().st_size > 0
    assert dst.read_bytes()[:6] == b"GIF89a"       # real GIF header
    assert _frame_count(dst) > 1                    # actually animated
    # 2s at 15fps -> ~30 frames.
    assert 25 <= _frame_count(dst) <= 35


@requires_ffmpeg
def test_width_is_capped(tmp_path):
    src = tmp_path / "big.mp4"
    dst = tmp_path / "big.gif"
    _make_silent_mp4(src, width=640, height=360)
    asyncio.run(mp4_to_gif(str(src), str(dst), max_width=480, fps=10, timeout=60))
    assert _dimensions(dst) == (480, 270)


@requires_ffmpeg
def test_small_source_is_not_upscaled(tmp_path):
    src = tmp_path / "small.mp4"
    dst = tmp_path / "small.gif"
    _make_silent_mp4(src, width=320, height=180)
    asyncio.run(mp4_to_gif(str(src), str(dst), max_width=480, fps=10, timeout=60))
    assert _dimensions(dst) == (320, 180)


@requires_ffmpeg
def test_missing_source_raises_gifconverror(tmp_path):
    with pytest.raises(GifConvError):
        asyncio.run(
            mp4_to_gif(str(tmp_path / "nope.mp4"), str(tmp_path / "o.gif"), timeout=30)
        )
