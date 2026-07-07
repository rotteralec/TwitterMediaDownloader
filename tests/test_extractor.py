"""Tests for app.extractor — the yt-dlp wrapper that classifies a tweet's media
as video vs GIF and picks download formats.

yt-dlp is never actually called: we monkeypatch ``extractor.YoutubeDL`` with a
fake that returns canned ``info`` dicts shaped like real yt-dlp Twitter output
(including the quirk that X's progressive ``http-*`` MP4s report *unknown*
codecs, and that GIFs are served from the ``tweet_video`` CDN path).
"""

import app.extractor as extractor
from app.extractor import ExtractError, _human_size, extract, normalize_url


# --- fake yt-dlp ------------------------------------------------------------

class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; returns FakeYDL.info from extract_info."""

    info: dict = {}

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return FakeYDL.info


class RaisingYDL:
    exc: Exception = RuntimeError("unset")

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise RaisingYDL.exc


def run(monkeypatch, info, url="https://x.com/user/status/1"):
    FakeYDL.info = info
    monkeypatch.setattr(extractor, "YoutubeDL", FakeYDL)
    return extract(url)


def run_raising(monkeypatch, exc, url="https://x.com/user/status/1"):
    RaisingYDL.exc = exc
    monkeypatch.setattr(extractor, "YoutubeDL", RaisingYDL)
    return extract(url)


# --- format-dict builders (mirror real yt-dlp Twitter output) ---------------

VIDEO_CDN = "https://video.twimg.com"


def vid_http(w, h, tbr):
    """Progressive MP4 for an ordinary video: unknown codecs, ext_tw_video path."""
    return {
        "format_id": f"http-{tbr}", "ext": "mp4", "protocol": "https",
        "vcodec": None, "acodec": None, "width": w, "height": h, "tbr": tbr,
        "url": f"{VIDEO_CDN}/ext_tw_video/1/pu/vid/{w}x{h}/v.mp4",
        "filesize": tbr * 2000,
    }


def vid_hls(w, h, tbr):
    """HLS video-only variant (acodec 'none', m3u8) — must be skipped."""
    return {
        "format_id": f"hls-{tbr}", "ext": "mp4", "protocol": "m3u8_native",
        "vcodec": "avc1.4D401E", "acodec": "none", "width": w, "height": h,
        "tbr": tbr, "url": f"{VIDEO_CDN}/ext_tw_video/1/pu/pl/{h}.m3u8",
    }


def hls_audio():
    """Separate audio track (real acodec), as X exposes for normal videos."""
    return {
        "format_id": "hls-audio-128000", "ext": "mp4", "protocol": "m3u8_native",
        "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 128,
        "url": f"{VIDEO_CDN}/ext_tw_video/1/pu/pl/audio.m3u8",
    }


def gif_http(w, h, tbr):
    """Progressive MP4 for a GIF: unknown codecs, tweet_video path, no audio."""
    return {
        "format_id": f"http-{tbr}", "ext": "mp4", "protocol": "https",
        "vcodec": None, "acodec": None, "width": w, "height": h, "tbr": tbr,
        "url": f"{VIDEO_CDN}/tweet_video/ABC{tbr}.mp4", "filesize": tbr * 1500,
    }


# A silent video (no audio anywhere), shaped like the real timClicks tweet.
SILENT_VIDEO = [
    vid_http(516, 270, 256), vid_hls(516, 270, 90),
    vid_http(690, 360, 832), vid_hls(690, 360, 269),
    vid_http(1082, 564, 2176), vid_hls(1082, 564, 442),
]


# --- classification ---------------------------------------------------------

def test_silent_video_is_video_not_gif(monkeypatch):
    """A silent clip on the ext_tw_video path is a VIDEO (MP4), never a GIF."""
    res = run(monkeypatch, {"title": "t", "formats": SILENT_VIDEO})
    item = res["media"][0]
    assert item["kind"] == "video"
    assert "source" not in item
    assert [f["quality"] for f in item["formats"]] == ["564p", "360p", "270p"]
    # Only progressive MP4s are offered — never the .m3u8 variants.
    assert all(f["url"].endswith(".mp4") for f in item["formats"])
    assert all(".m3u8" not in f["url"] for f in item["formats"])


def test_genuine_gif_is_gif(monkeypatch):
    """A clip on the tweet_video path is a GIF, with a source to transcode."""
    res = run(monkeypatch, {"title": "g", "formats": [gif_http(480, 270, 600)]})
    item = res["media"][0]
    assert item["kind"] == "gif"
    assert "formats" not in item
    assert "tweet_video" in item["source"]["url"]
    assert item["width"] == 480 and item["source"]["height"] == 270


def test_gif_with_unknown_codecs_still_detected(monkeypatch):
    """Regression: X's GIF MP4s report vcodec/acodec as unknown (None). They
    must still be picked (the earlier bug discarded them)."""
    fmt = gif_http(480, 270, 600)
    assert fmt["vcodec"] is None and fmt["acodec"] is None
    res = run(monkeypatch, {"title": "g", "formats": [fmt]})
    assert res["media"][0]["kind"] == "gif"
    assert res["media"][0]["source"]["url"].endswith(".mp4")


def test_normal_video_with_audio_is_video(monkeypatch):
    formats = [hls_audio(), vid_http(1280, 720, 2500)]
    res = run(monkeypatch, {"title": "v", "formats": formats})
    item = res["media"][0]
    assert item["kind"] == "video"
    assert [f["quality"] for f in item["formats"]] == ["720p"]


def test_video_quality_options_capped_to_three_with_spread(monkeypatch):
    """More than 3 resolutions collapse to highest / middle / lowest."""
    formats = [
        vid_http(640, 360, 800), vid_http(854, 480, 1200),
        vid_http(1280, 720, 2500), vid_http(1920, 1080, 5000),
    ]
    res = run(monkeypatch, {"title": "v", "formats": formats})
    assert [f["quality"] for f in res["media"][0]["formats"]] == ["1080p", "480p", "360p"]


def test_mixed_playlist_video_then_gif(monkeypatch):
    info = {
        "title": "thread", "uploader": "u", "_type": "playlist",
        "entries": [
            {"title": "a", "formats": SILENT_VIDEO},
            {"title": "b", "formats": [gif_http(480, 270, 600)]},
        ],
    }
    res = run(monkeypatch, info)
    assert [m["kind"] for m in res["media"]] == ["video", "gif"]


def test_hls_only_video_yields_no_media(monkeypatch):
    """A video exposed only as HLS (no progressive MP4) can't be proxied, so we
    report no downloadable media rather than hand back an unplayable .m3u8."""
    formats = [hls_audio(), vid_hls(1280, 720, 2500)]
    try:
        run(monkeypatch, {"title": "v", "formats": formats})
        assert False, "expected ExtractError"
    except ExtractError as e:
        assert e.code == "no_media"


def test_photo_only_tweet_has_no_media(monkeypatch):
    try:
        run(monkeypatch, {"title": "pic", "formats": []})
        assert False, "expected ExtractError"
    except ExtractError as e:
        assert e.code == "no_media"


# --- error mapping ----------------------------------------------------------

def test_private_account_maps_to_private(monkeypatch):
    try:
        run_raising(monkeypatch, extractor.DownloadError("This account is protected"))
        assert False
    except ExtractError as e:
        assert e.code == "private"


def test_deleted_tweet_maps_to_not_found(monkeypatch):
    try:
        run_raising(monkeypatch, extractor.DownloadError("HTTP Error 404: Not Found"))
        assert False
    except ExtractError as e:
        assert e.code == "not_found"


# --- URL + helpers ----------------------------------------------------------

def test_normalize_url_strips_query_and_forces_https():
    assert normalize_url("http://x.com/u/status/123?s=20") == "https://x.com/u/status/123"
    assert normalize_url("twitter.com/u/status/9") == "https://twitter.com/u/status/9"


def test_normalize_url_rejects_non_tweet():
    for bad in ("", "https://youtube.com/watch?v=x", "https://x.com/u"):
        try:
            normalize_url(bad)
            assert False, f"should reject {bad!r}"
        except ExtractError as e:
            assert e.code == "invalid_url"


def test_human_size():
    assert _human_size(None) is None
    assert _human_size(0) is None
    assert _human_size(900) == "900 B"
    assert _human_size(2_500_000) == "2.4 MB"
