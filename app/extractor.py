"""yt-dlp wrapper that pulls video / GIF info from an X / Twitter status URL.

X has three motion cases we care about, and they must be handled differently:
  * **Videos** — ordinary uploads. Served from the ``ext_tw_video`` /
    ``amplify_video`` CDN paths as progressive MP4s (plus HLS). Download as MP4.
  * **Silent videos** — a normal video that just has no audio track. Still a
    video: it must download as MP4, NOT be converted to a GIF.
  * **GIFs** — when you post a GIF, X transcodes it to a short, silent, looping
    MP4 served from the ``tweet_video`` CDN path (the ``animated_gif`` media
    type). Only these should become a real ``.gif``.

So we classify by the CDN path, NOT by "has audio" — silent videos also lack
audio, so audio can't distinguish them. A clip is treated as a GIF only when its
media positively comes from ``tweet_video``; anything else is a video. That bias
is deliberate: the worst case is a GIF downloading as MP4, never a (silent) video
being wrongly turned into a GIF.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

# Accept twitter.com, x.com, mobile.twitter.com, www.* — must point at a /status/<id>.
_STATUS_RE = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.)?(?:twitter|x)\.com/[^/]+/status/(\d+)",
    re.IGNORECASE,
)

# How many quality options to surface for a video (highest / middle / lowest).
_MAX_VIDEO_OPTIONS = 3


class ExtractError(Exception):
    """Raised for any user-facing extraction failure."""

    def __init__(self, message: str, code: str = "extract_failed"):
        super().__init__(message)
        self.code = code


def normalize_url(raw: str) -> str:
    """Strip query / fragment, force https, validate it looks like a tweet URL."""
    if not raw or not isinstance(raw, str):
        raise ExtractError("Please paste a tweet URL.", code="invalid_url")
    raw = raw.strip()
    if not _STATUS_RE.match(raw):
        raise ExtractError(
            "That doesn't look like a tweet URL. Expected something like "
            "https://x.com/username/status/1234567890.",
            code="invalid_url",
        )
    parsed = urlparse(raw if raw.startswith("http") else f"https://{raw}")
    # Drop query + fragment; we only need the path.
    cleaned = parsed._replace(scheme="https", query="", fragment="")
    return urlunparse(cleaned)


def _human_size(n: int | None) -> str | None:
    if not n or n <= 0:
        return None
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _is_hls(f: dict[str, Any]) -> bool:
    """HLS / m3u8 formats aren't standalone files — our download proxy can't
    stream them as a single MP4, so we skip them everywhere."""
    proto = (f.get("protocol") or "").lower()
    if "m3u8" in proto:
        return True
    return ".m3u8" in (f.get("url") or "").lower()


# X serves GIFs (the animated_gif media type) from this CDN path; ordinary
# videos use ext_tw_video / amplify_video. This is how we tell a genuine GIF
# apart from a merely-silent video.
_TWEET_VIDEO_RE = re.compile(r"/tweet_video/", re.IGNORECASE)


def _looks_like_gif(formats: list[dict[str, Any]]) -> bool:
    """True only for a genuine X GIF (animated_gif), identified by its
    ``tweet_video`` CDN path. A silent *video* is NOT a GIF — it's an ordinary
    video that happens to have no audio and must download as MP4. We only return
    True on a positive GIF match; everything else defaults to video."""
    return any(_TWEET_VIDEO_RE.search(f.get("url") or "") for f in formats)


def _pick_muxed_mp4s(formats: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Progressive (non-HLS) MP4s suitable as a standalone video download,
    deduped by height (keeping the higher-bitrate option per resolution).

    Important: X's downloadable ``http-*`` MP4s come back from yt-dlp with BOTH
    codecs reported as unknown (``vcodec``/``acodec`` == ``None``) — those are the
    files we actually want. So we keep formats with unknown codecs and only drop
    ones that are *explicitly* video-only (``acodec == "none"``) or audio-only
    (``vcodec == "none"``); those explicit-none entries are the HLS variants.
    (Video-vs-GIF classification is decided separately by ``_entry_has_any_audio``
    using real audio evidence.)"""
    candidates: dict[int, dict[str, Any]] = {}
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none":  # audio-only -> skip (keep unknown vcodec)
            continue
        if f.get("acodec") == "none":  # explicitly video-only -> skip
            continue
        if _is_hls(f):
            continue
        if not f.get("url"):
            continue
        height = f.get("height") or 0
        existing = candidates.get(height)
        if not existing or (f.get("tbr") or 0) > (existing.get("tbr") or 0):
            candidates[height] = f
    return candidates


def _pick_gif_source(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best progressive (non-HLS) MP4 for a GIF: highest resolution, then
    highest bitrate. Audio is irrelevant — GIFs are silent by definition. As with
    videos, X's ``http-*`` MP4s report an unknown ``vcodec``, so we keep unknown
    and only skip formats that are explicitly audio-only (``vcodec == "none"``)."""
    best: dict[str, Any] | None = None
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none":  # audio-only -> skip (keep unknown vcodec)
            continue
        if _is_hls(f):
            continue
        if not f.get("url"):
            continue
        if best is None:
            best = f
            continue
        bh, fh = best.get("height") or 0, f.get("height") or 0
        if fh > bh or (fh == bh and (f.get("tbr") or 0) > (best.get("tbr") or 0)):
            best = f
    return best


def _spread_options(sorted_desc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce a resolution-sorted list to at most _MAX_VIDEO_OPTIONS, keeping a
    useful spread (highest, a middle, lowest) rather than three near-identical
    top resolutions."""
    n = len(sorted_desc)
    if n <= _MAX_VIDEO_OPTIONS:
        return sorted_desc
    if _MAX_VIDEO_OPTIONS == 3:
        return [sorted_desc[0], sorted_desc[n // 2], sorted_desc[-1]]
    # General fallback: evenly sample indices across the range.
    step = (n - 1) / (_MAX_VIDEO_OPTIONS - 1)
    idxs = sorted({round(i * step) for i in range(_MAX_VIDEO_OPTIONS)})
    return [sorted_desc[i] for i in idxs]


def _video_options(muxed: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    chosen = sorted(muxed.values(), key=lambda x: x.get("height") or 0, reverse=True)
    chosen = _spread_options(chosen)
    out: list[dict[str, Any]] = []
    for f in chosen:
        h = f.get("height")
        quality = f"{h}p" if h else (f.get("format_note") or "Original")
        size = f.get("filesize") or f.get("filesize_approx")
        out.append(
            {
                "quality": quality,
                "height": h,
                "ext": "mp4",
                "url": f["url"],
                "filesize": size,
                "filesize_human": _human_size(size),
            }
        )
    return out


def extract(url: str) -> dict[str, Any]:
    """Resolve a tweet URL to downloadable media.

    Returns a dict shaped like:
        {
          "title": "...",
          "uploader": "...",
          "thumbnail": "...",
          "media": [
            # a video entry:
            { "kind": "video", "title": "...", "duration": 12.3,
              "thumbnail": "...",
              "formats": [ {quality, height, ext, url, filesize, filesize_human}, ... ] },
            # a gif entry:
            { "kind": "gif", "title": "...", "duration": 3.1, "thumbnail": "...",
              "width": 480, "height": 270,
              "source": {url, height, filesize, filesize_human} },
            ...
          ]
        }
    """
    clean = normalize_url(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,  # let multi-video tweets come through
        "extract_flat": False,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean, download=False)
    except DownloadError as e:
        msg = str(e).lower()
        if "private" in msg or "protected" in msg:
            raise ExtractError(
                "This tweet is from a private/protected account.", code="private"
            ) from e
        if (
            "no video" in msg
            or "does not contain a video" in msg
            or "no media" in msg
            or "unsupported" in msg
        ):
            raise ExtractError(
                "No video or GIF found in that tweet.", code="no_media"
            ) from e
        if "not found" in msg or "no longer exists" in msg or "404" in msg:
            raise ExtractError("That tweet doesn't exist or was deleted.", code="not_found") from e
        if "rate" in msg or "429" in msg:
            raise ExtractError(
                "X is rate-limiting us right now. Try again in a minute.", code="upstream_rate"
            ) from e
        raise ExtractError("Couldn't fetch that tweet. Try again.", code="extract_failed") from e
    except ExtractorError as e:
        raise ExtractError("Couldn't fetch that tweet. Try again.", code="extract_failed") from e

    if not info:
        raise ExtractError("No video or GIF found in that tweet.", code="no_media")

    # Normalize single-item vs playlist (multi-item) results.
    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    media: list[dict[str, Any]] = []
    for entry in entries:
        if not entry:
            continue
        formats = entry.get("formats") or []
        if not formats:
            continue

        if _looks_like_gif(formats):
            # Genuine X GIF (tweet_video path) => transcode to a real .gif later.
            src = _pick_gif_source(formats)
            if not src:
                continue
            size = src.get("filesize") or src.get("filesize_approx")
            media.append(
                {
                    "kind": "gif",
                    "title": entry.get("title") or "Twitter GIF",
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                    "width": src.get("width"),
                    "height": src.get("height"),
                    "source": {
                        "url": src["url"],
                        "height": src.get("height"),
                        "filesize": size,
                        "filesize_human": _human_size(size),
                    },
                }
            )
        else:
            # Everything else is a video — INCLUDING a silent video, which must
            # download as MP4, not be converted to a GIF. Offer MP4 qualities.
            muxed = _pick_muxed_mp4s(formats)
            if not muxed:
                continue
            media.append(
                {
                    "kind": "video",
                    "title": entry.get("title") or "Twitter video",
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                    "formats": _video_options(muxed),
                }
            )

    if not media:
        raise ExtractError("No downloadable video or GIF found in that tweet.", code="no_media")

    return {
        "title": info.get("title") or media[0]["title"],
        "uploader": info.get("uploader") or info.get("uploader_id"),
        "thumbnail": info.get("thumbnail") or media[0].get("thumbnail"),
        "media": media,
    }
