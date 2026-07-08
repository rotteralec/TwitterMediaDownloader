"""FastAPI app: extract tweet video info, proxy the download.

Owner mode: visit `/?key=<OWNER_KEY>` once to set a cookie that bypasses the
daily limit. Set the OWNER_KEY env var to enable.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import Cookie, FastAPI, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .extractor import ExtractError, extract
from .gifconv import GifConvError, ffmpeg_available, mp4_to_gif
from .rate_limit import RateLimiter

# --- Config (env-driven) ----------------------------------------------------

OWNER_KEY = os.environ.get("OWNER_KEY", "").strip()
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "3"))
GITHUB_URL = os.environ.get(
    "GITHUB_URL", "https://github.com/your-username/twitter-video-downloader"
)

# --- GIF conversion (env-driven) --------------------------------------------
# GIFs are transcoded from X's silent MP4 with ffmpeg at download time. These
# bounds keep output sizes mobile-friendly and protect the server from being
# asked to transcode something huge.
GIF_MAX_WIDTH = int(os.environ.get("GIF_MAX_WIDTH", "480"))   # px; never upscales
GIF_FPS = int(os.environ.get("GIF_FPS", "15"))               # frames/sec in the GIF
GIF_MAX_SOURCE_MB = int(os.environ.get("GIF_MAX_SOURCE_MB", "50"))  # reject bigger sources
GIF_TIMEOUT = float(os.environ.get("GIF_TIMEOUT", "90"))     # seconds per ffmpeg run
COOKIE_NAME = "owner_token"
# Set COOKIE_SECURE=true when serving over HTTPS so the owner cookie is only
# sent on encrypted connections. Defaults to false for local/plain-HTTP use.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
# A random per-process token. If OWNER_KEY is set, anyone presenting ?key=OWNER_KEY
# gets this token in a cookie; the server only trusts the token, not the key.
_OWNER_TOKEN = secrets.token_urlsafe(32) if OWNER_KEY else ""

# --- App --------------------------------------------------------------------

app = FastAPI(title="Twitter Video Downloader", docs_url=None, redoc_url=None)
limiter = RateLimiter(DAILY_LIMIT)

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


def _strip_port(host: str) -> str:
    """Return just the IP from an X-Forwarded-For element that may carry a port.
    Handles 'a.b.c.d:port', bracketed IPv6 '[::1]:port', and bare IPs."""
    host = host.strip()
    if host.startswith("["):              # [IPv6]:port
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:              # IPv4:port
        return host.rsplit(":", 1)[0]
    return host                            # bare IPv4 or bare/unbracketed IPv6


def _client_ip(req: Request) -> str:
    # Behind Azure App Service's front end (one trusted hop), the real client IP is
    # the RIGHTMOST X-Forwarded-For value: the front end appends it after anything
    # the caller sent, so values to its left are attacker-controlled. Never take the
    # leftmost. If XFF is absent (local dev), fall back to the socket peer.
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        ip = _strip_port(fwd.split(",")[-1])
        if ip:
            return ip
    return req.client.host if req.client else "unknown"


def _is_owner(token: Optional[str]) -> bool:
    return bool(_OWNER_TOKEN) and token == _OWNER_TOKEN


# --- Routes -----------------------------------------------------------------


class ExtractReq(BaseModel):
    url: str


@app.get("/")
async def index(
    response: Response,
    key: Optional[str] = Query(default=None),
):
    """Serve the SPA. If ?key=<OWNER_KEY> is passed, set the owner cookie and redirect."""
    if key is not None:
        if OWNER_KEY and secrets.compare_digest(key, OWNER_KEY):
            resp = RedirectResponse(url="/", status_code=303)
            # 1 year cookie. httponly so JS can't read it; samesite=lax for normal nav.
            resp.set_cookie(
                COOKIE_NAME,
                _OWNER_TOKEN,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                samesite="lax",
                secure=COOKIE_SECURE,  # env-driven: COOKIE_SECURE=true behind HTTPS
            )
            return resp
        # Wrong key → just serve the page without setting anything.
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/status")
async def status(request: Request, owner_token: Optional[str] = Cookie(default=None)):
    is_owner = _is_owner(owner_token)
    return {
        "owner": is_owner,
        "daily_limit": DAILY_LIMIT,
        "remaining": (None if is_owner else limiter.remaining(_client_ip(request))),
        "github_url": GITHUB_URL,
    }


@app.post("/api/extract")
async def api_extract(
    body: ExtractReq,
    request: Request,
    owner_token: Optional[str] = Cookie(default=None),
):
    is_owner = _is_owner(owner_token)

    if not is_owner:
        ip = _client_ip(request)
        allowed, remaining = limiter.check(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        f"You've hit today's free limit of {DAILY_LIMIT} downloads. "
                        "Want unlimited? Host your own copy — it's open source."
                    ),
                    "github_url": GITHUB_URL,
                },
            )
    else:
        remaining = None

    try:
        info = extract(body.url)
    except ExtractError as e:
        # If we charged a count on a failed extraction, that feels harsh — refund.
        # (We only refund client-side validation errors. Server errors keep the count
        # so people can't spam invalid-but-real-looking URLs.)
        if not is_owner and e.code in ("invalid_url",):
            # Best-effort refund: decrement by serving a fresh allowed check is messy.
            # Simpler: validate URL first, before charging. (See refactor below.)
            pass
        return JSONResponse(
            status_code=400 if e.code == "invalid_url" else 422,
            content={"error": e.code, "message": str(e)},
        )

    info["remaining"] = remaining
    info["owner"] = is_owner
    return info


_FNAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, ext: str = "mp4") -> str:
    base = _FNAME_SAFE.sub("_", name).strip("_") or "twitter_video"
    base = base[:80]
    return f"{base}.{ext.lstrip('.')}"


async def _download_as_gif(url: str, filename: Optional[str]):
    """Download the (silent) source MP4 to a temp file, transcode it to a real
    animated GIF with ffmpeg, then stream the GIF back as an attachment."""
    if not ffmpeg_available():
        return JSONResponse(
            status_code=503,
            content={
                "error": "no_ffmpeg",
                "message": "GIF conversion isn't available on this server.",
            },
        )

    out_name = _safe_filename(filename or "twitter_gif", ext="gif")
    max_bytes = GIF_MAX_SOURCE_MB * 1024 * 1024
    tmpdir = tempfile.mkdtemp(prefix="twvd_gif_")
    src_path = os.path.join(tmpdir, "source.mp4")
    gif_path = os.path.join(tmpdir, "out.gif")

    def fail(status: int, code: str, message: str):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(status_code=status, content={"error": code, "message": message})

    too_large = f"That clip is too large to convert to GIF (over {GIF_MAX_SOURCE_MB} MB)."

    # 1) Pull the source MP4 to a temp file, enforcing a size cap as we go.
    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    try:
        async with client.stream("GET", url) as upstream:
            if upstream.status_code >= 400:
                return fail(502, "upstream", f"CDN returned {upstream.status_code}.")
            declared = upstream.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return fail(413, "too_large", too_large)
            total = 0
            with open(src_path, "wb") as fp:
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        return fail(413, "too_large", too_large)
                    fp.write(chunk)
    except httpx.HTTPError:
        return fail(502, "upstream", "Couldn't reach Twitter's CDN.")
    finally:
        await client.aclose()

    # 2) Transcode MP4 -> GIF.
    try:
        await mp4_to_gif(
            src_path,
            gif_path,
            max_width=GIF_MAX_WIDTH,
            fps=GIF_FPS,
            timeout=GIF_TIMEOUT,
        )
    except GifConvError as e:
        status = {"no_ffmpeg": 503, "gif_timeout": 504}.get(e.code, 502)
        return fail(status, e.code, str(e))

    # 3) Stream the finished GIF, cleaning up the temp dir when done.
    gif_size = os.path.getsize(gif_path)

    async def streamer():
        try:
            with open(gif_path, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{out_name}"',
        "Cache-Control": "no-store",
        "Content-Length": str(gif_size),
    }
    return StreamingResponse(streamer(), media_type="image/gif", headers=headers)


def _is_allowed_cdn(url: str) -> bool:
    """True only for https URLs whose host is twimg.com or a *.twimg.subdomain.
        Uses parsed hostname (not a substring) so look-alikes such as faketwimg.com are rejected.
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "twimg.com" or host.endswith(".twimg.com"))

@app.get("/api/download")
async def api_download(
    url: str = Query(...),
    filename: Optional[str] = Query(default=None),
    fmt: str = Query(default="mp4"),
):
    """Stream a Twitter CDN file back with Content-Disposition: attachment.

    ``fmt="mp4"`` (default) passes the MP4 through untouched; ``fmt="gif"``
    downloads the silent source MP4 and transcodes it to a real animated GIF.

    We only allow URLs from Twitter's video CDN to avoid being a generic open proxy.
    """
    if not _is_allowed_cdn(url):
        return JSONResponse(
            status_code=400,
            content={"error": "bad_url", "message": "Only twimg.com URLs are allowed."},
        )

    if fmt == "gif":
        return await _download_as_gif(url, filename)

    out_name = _safe_filename(filename or "twitter_video", ext="mp4")

    # Stream chunked so large files don't blow up memory.
    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    try:
        upstream = await client.send(
            client.build_request("GET", url),
            stream=True,
        )
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"error": "upstream", "message": "Couldn't reach Twitter's CDN."},
        )

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"error": "upstream", "message": f"CDN returned {upstream.status_code}."},
        )

    async def streamer():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": f'attachment; filename="{out_name}"',
        "Cache-Control": "no-store",
    }
    content_length = upstream.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length

    return StreamingResponse(
        streamer(),
        media_type="video/mp4",
        headers=headers,
    )


# Serve static assets with "no-cache" so edits show up on a normal refresh
# instead of the browser serving a stale app.js / styles.css. "no-cache" makes
# the browser revalidate via ETag, so it's still cheap (304s) but never stale.
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Mount /static/* for css/js. Index is served from "/" above so we control caching.
app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")
