"""Convert a (silent) MP4 to an animated GIF with ffmpeg.

X stores "GIFs" as silent MP4s, so to hand back a real .gif we transcode. We use
the standard two-pass palette technique (palettegen + paletteuse) because a flat
256-colour GIF without a per-clip palette looks badly banded.

ffmpeg is invoked as an async subprocess so it doesn't block the event loop, and
every run is bounded by a timeout. ffmpeg is a system dependency (already in the
Docker image); call `ffmpeg_available()` before relying on conversion.
"""

from __future__ import annotations

import asyncio
import os
import shutil


class GifConvError(Exception):
    """User-facing GIF-conversion failure."""

    def __init__(self, message: str, code: str = "gif_failed"):
        super().__init__(message)
        self.code = code


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _scale_expr(max_width: int) -> str:
    # Cap width to max_width but never upscale a smaller source; keep height even
    # (-2) for broad encoder compatibility. Commas inside min() are escaped so
    # ffmpeg doesn't read them as filter separators.
    return f"scale=w='min({max_width}\\,iw)':h=-2:flags=lanczos"


async def _run(cmd: list[str], timeout: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise GifConvError(
            "GIF conversion took too long and was stopped.", code="gif_timeout"
        ) from e
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = detail[-1] if detail else "unknown error"
        raise GifConvError(f"Couldn't convert this clip to a GIF ({tail}).")


async def mp4_to_gif(
    src_path: str,
    dst_path: str,
    *,
    max_width: int = 480,
    fps: int = 15,
    timeout: float = 90.0,
) -> None:
    """Transcode the MP4 at ``src_path`` into a looping GIF at ``dst_path``.

    Raises ``GifConvError`` (with a ``.code``) on any failure. The caller owns
    both paths and is responsible for cleaning up the directory afterwards.
    """
    if not ffmpeg_available():
        raise GifConvError(
            "GIF conversion isn't available on this server (ffmpeg is missing).",
            code="no_ffmpeg",
        )

    vf = f"fps={fps},{_scale_expr(max_width)}"
    palette_path = dst_path + ".palette.png"

    # Pass 1: build an optimal colour palette for this specific clip.
    pass1 = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-vf", f"{vf},palettegen=stats_mode=diff",
        "-frames:v", "1",
        palette_path,
    ]
    # Pass 2: render the GIF using that palette; loop forever (-loop 0).
    pass2 = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-i", palette_path,
        "-lavfi", f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
        "-loop", "0",
        dst_path,
    ]

    try:
        await _run(pass1, timeout)
        await _run(pass2, timeout)
    finally:
        try:
            os.remove(palette_path)
        except OSError:
            pass

    if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
        raise GifConvError("GIF conversion produced an empty file.")
