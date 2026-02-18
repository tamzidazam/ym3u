"""
YT-M3U8 API v3
- Proxies ALL HLS content through our server (fixes CORS + referrer blocks)
- Handles live streams (hls_manifest_url) AND VOD
- Segments are fetched server-side and streamed to client
"""
import os
import re
import httpx
import asyncio
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="YT-M3U8 API", description="YouTube → proxied HLS streams", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY     = os.environ.get("API_KEY", "")
_default_cookies_dir = Path(__file__).parent / "data"
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", str(_default_cookies_dir)))
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.youtube.com",
    "Referer": "https://www.youtube.com/",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_cookies() -> str | None:
    for p in [str(COOKIES_DIR / "cookies.txt"), os.environ.get("COOKIES_FILE", ""), "./cookies.txt"]:
        if p and Path(p).exists() and Path(p).stat().st_size > 0:
            return p
    return None


def get_ydl_opts(fmt="best") -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "http_headers": PROXY_HEADERS,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
    cookie = find_cookies()
    if cookie:
        opts["cookiefile"] = cookie
    return opts


def check_key(request: Request):
    if not API_KEY:
        return
    k = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if k != API_KEY:
        raise HTTPException(401, "Invalid or missing API key.")


def extract_info(url: str, fmt="best"):
    with yt_dlp.YoutubeDL(get_ydl_opts(fmt)) as ydl:
        return ydl.extract_info(url, download=False)


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "Sign in to confirm" in msg or "bot" in msg.lower():
        return "YouTube bot-detection triggered. Upload cookies via POST /api/cookies (see /docs)."
    if "age" in msg.lower():
        return "Age-restricted — upload YouTube cookies via POST /api/cookies."
    if "Private" in msg:
        return "Private video — needs cookies from an account with access."
    if "members" in msg.lower():
        return "Members-only — needs cookies from a member account."
    if "not available" in msg.lower():
        return f"Format not available: {msg}"
    return msg


def make_proxy_url(base_request: Request, target_url: str) -> str:
    """Encode a remote URL so it goes through our /proxy endpoint."""
    encoded = quote(target_url, safe="")
    base = str(base_request.base_url).rstrip("/")
    return f"{base}/proxy?url={encoded}"


def rewrite_m3u8(content: str, base_url: str, request: Request) -> str:
    """
    Rewrite an M3U8 playlist so all URLs (segment, key, sub-playlist)
    are routed through our /proxy endpoint.
    """
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue

        # Handle URI= attributes in tags (EXT-X-KEY, EXT-X-MAP, etc.)
        def replace_uri(m):
            uri = m.group(1)
            abs_url = urljoin(base_url, uri)
            return f'URI="{make_proxy_url(request, abs_url)}"'

        rewritten = re.sub(r'URI="([^"]+)"', replace_uri, line)

        # Handle bare URLs (segment lines — don't start with #)
        if not stripped.startswith("#"):
            abs_url = urljoin(base_url, stripped)
            rewritten = make_proxy_url(request, abs_url)

        out.append(rewritten)

    return "\n".join(out)


async def fetch_and_rewrite_m3u8(manifest_url: str, request: Request) -> str:
    """Fetch a remote M3U8 and rewrite all URLs through our proxy."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        r = await client.get(manifest_url, headers=PROXY_HEADERS)
        r.raise_for_status()
        content = r.text
        return rewrite_m3u8(content, manifest_url, request)


def build_vod_m3u8(info: dict, quality: str, request: Request) -> str:
    """Build a proxied M3U8 for a VOD (non-live) video."""
    formats = info.get("formats", [])
    vf = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
    if not vf:
        raise HTTPException(404, "No playable formats found")

    if quality == "best":
        vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
    elif quality == "worst":
        vf.sort(key=lambda x: x.get("height", 0) or 0)
    else:
        try:
            t = int(quality)
            vf.sort(key=lambda x: abs((x.get("height", 0) or 0) - t))
        except ValueError:
            vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)

    chosen = vf[0]
    stream_url = chosen["url"]
    dur = info.get("duration", 0) or 0
    title = info.get("title", "video")

    # Proxy the stream URL through our server
    proxied = make_proxy_url(request, stream_url)

    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{int(dur)+1}\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{dur:.3f},{title}\n"
        f"{proxied}\n"
        "#EXT-X-ENDLIST\n"
    )
    return playlist


# ── Proxy endpoint ────────────────────────────────────────────────────────────

@app.get("/proxy", include_in_schema=False)
async def proxy(request: Request, url: str = Query(...)):
    """
    Generic reverse proxy for HLS segments, manifests, and encryption keys.
    Rewrites M3U8 content on the fly. Streams binary segments directly.
    """
    target = unquote(url)

    # Safety: only proxy googlevideo / googleusercontent / YouTube domains
    allowed = ("googlevideo.com", "googleusercontent.com", "youtube.com",
               "ytimg.com", "googlevideo.com", "manifest.googlevideo.com")
    host = urlparse(target).netloc
    if not any(host.endswith(a) for a in allowed):
        raise HTTPException(403, f"Proxy not allowed for host: {host}")

    async def stream_response():
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", target, headers=PROXY_HEADERS) as r:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    # Check content type first to decide if we need to rewrite
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        head = await client.head(target, headers=PROXY_HEADERS)
        ct = head.headers.get("content-type", "")

    is_m3u8 = "mpegurl" in ct or "m3u8" in ct or target.split("?")[0].endswith(".m3u8")

    if is_m3u8:
        # Fetch, rewrite URLs, return text
        rewritten = await fetch_and_rewrite_m3u8(target, request)
        return PlainTextResponse(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # Binary content (TS segments, MP4 fragments, keys) — stream directly
    return StreamingResponse(
        stream_response(),
        media_type=ct or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Cookie management ─────────────────────────────────────────────────────────

@app.post("/api/cookies", summary="Upload cookies.txt", tags=["Setup"],
    description="Upload Netscape cookies.txt from your browser. Fixes bot-detection errors.")
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    check_key(request)
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    text = content.decode("utf-8", errors="replace")
    if "# Netscape HTTP Cookie File" not in text and "youtube" not in text.lower():
        raise HTTPException(400, "Not a valid Netscape cookies.txt from YouTube.")
    dest = COOKIES_DIR / "cookies.txt"
    dest.write_bytes(content)
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    return {"status": "ok", "entries": len(lines), "has_youtube": "youtube.com" in text}


@app.get("/api/cookies/status", summary="Cookie status", tags=["Setup"])
async def cookies_status(request: Request):
    check_key(request)
    path = find_cookies()
    if not path:
        return {"cookies_loaded": False,
                "fix": "POST cookies.txt to /api/cookies (export from YouTube via 'Get cookies.txt LOCALLY')"}
    text = Path(path).read_text(errors="replace")
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    return {"cookies_loaded": True, "entries": len(lines),
            "size_kb": Path(path).stat().st_size // 1024,
            "has_youtube_cookies": "youtube.com" in text}


@app.delete("/api/cookies", summary="Delete cookies", tags=["Setup"])
async def delete_cookies(request: Request):
    check_key(request)
    p = find_cookies()
    if p:
        os.remove(p)
        return {"status": "ok", "message": "Cookies deleted"}
    return {"status": "ok", "message": "No cookies were loaded"}


# ── Core API ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    p = Path("static/index.html")
    return p.read_text() if p.exists() else "<h1>YT-M3U8 API v3</h1><a href='/docs'>Docs</a>"


@app.get("/api/m3u8", response_class=PlainTextResponse,
         summary="Get proxied M3U8 playlist", tags=["Core"])
async def get_m3u8(
    request: Request,
    url: str = Query(..., description="YouTube video URL", example="https://youtube.com/watch?v=dQw4w9WgXcQ"),
    quality: str = Query("best", description="best | worst | 1080 | 720 | 480 | 360"),
):
    """
    Returns a proxied M3U8 playlist — all segment URLs go through this server.
    Works in browsers (CORS fixed), VLC, mpv, ffmpeg, and HLS.js.
    Live streams are fully supported.
    """
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    is_live = info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")
    manifest_url = info.get("manifest_url") or info.get("hls_manifest_url")

    if is_live and manifest_url:
        # Live stream: fetch master manifest and rewrite all URLs through proxy
        try:
            rewritten = await fetch_and_rewrite_m3u8(manifest_url, request)
        except Exception as e:
            raise HTTPException(502, f"Failed to fetch live manifest: {e}")
        return PlainTextResponse(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
        )

    # Also check for HLS formats in format list
    formats = info.get("formats", [])
    hls_formats = [f for f in formats if f.get("protocol") in ("m3u8", "m3u8_native") and f.get("url")]

    if hls_formats:
        # Pick the best HLS format and proxy its manifest
        if quality == "best":
            hls_formats.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
        elif quality == "worst":
            hls_formats.sort(key=lambda x: x.get("height", 0) or 0)
        else:
            try:
                t = int(quality)
                hls_formats.sort(key=lambda x: abs((x.get("height", 0) or 0) - t))
            except ValueError:
                hls_formats.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)

        chosen_hls = hls_formats[0]
        try:
            rewritten = await fetch_and_rewrite_m3u8(chosen_hls["url"], request)
            return PlainTextResponse(
                rewritten,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
            )
        except Exception:
            pass  # Fall through to VOD path

    # VOD: build synthetic M3U8 with proxied segment URL
    playlist = build_vod_m3u8(info, quality, request)
    return PlainTextResponse(
        playlist,
        media_type="application/vnd.apple.mpegurl",
        headers={"Content-Disposition": 'inline; filename="playlist.m3u8"',
                 "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/formats", summary="List formats", tags=["Info"])
async def get_formats(request: Request, url: str = Query(...)):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))

    is_live = info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")
    seen, formats = set(), []
    for f in info.get("formats", []):
        h = f.get("height")
        if h and h not in seen and f.get("vcodec") not in ("none", None):
            seen.add(h)
            formats.append({
                "quality": f"{h}p", "height": h, "width": f.get("width"),
                "fps": f.get("fps"), "protocol": f.get("protocol"),
                "has_audio": f.get("acodec") not in ("none", None),
                "m3u8_url": f"{request.base_url}api/m3u8?url={url}&quality={h}",
            })
    formats.sort(key=lambda x: x["height"], reverse=True)
    return {
        "title": info.get("title"),
        "is_live": is_live,
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "available_qualities": formats,
    }


@app.get("/api/stream-url", summary="Raw stream URL (no proxy)", tags=["Core"])
async def get_stream_url(request: Request, url: str = Query(...), quality: str = Query("best")):
    """Direct signed URL — expires ~6h. Re-call to refresh. Won't work in browsers due to CORS."""
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))

    formats = info.get("formats", [])
    vf = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
    if not vf:
        raise HTTPException(404, "No formats found")
    if quality == "best":
        vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
    elif quality == "worst":
        vf.sort(key=lambda x: x.get("height", 0) or 0)
    else:
        try:
            t = int(quality)
            vf.sort(key=lambda x: abs((x.get("height", 0) or 0) - t))
        except ValueError:
            vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
    chosen = vf[0]
    return {"title": info.get("title"), "quality": f"{chosen.get('height','?')}p",
            "ext": chosen.get("ext"), "url": chosen["url"],
            "note": "Direct URL expires ~6h. Use /api/m3u8 for a stable proxied link."}


@app.get("/api/info", summary="Full video metadata", tags=["Info"])
async def get_info(request: Request, url: str = Query(...)):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    formats = [{"format_id": f.get("format_id"), "ext": f.get("ext"), "height": f.get("height"),
                "width": f.get("width"), "fps": f.get("fps"), "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"), "tbr": f.get("tbr"), "protocol": f.get("protocol")}
               for f in info.get("formats", [])]
    return {"id": info.get("id"), "title": info.get("title"),
            "is_live": info.get("is_live"), "live_status": info.get("live_status"),
            "description": (info.get("description") or "")[:500],
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"), "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"), "like_count": info.get("like_count"),
            "formats_count": len(formats), "formats": formats}


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "yt_dlp_version": yt_dlp.version.__version__,
            "cookies_loaded": find_cookies() is not None}


if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")
