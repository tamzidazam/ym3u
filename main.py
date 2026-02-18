"""
YT-M3U8 API v4
- Uses yt-dlp for ALL YouTube fetching (handles IP-locked signed URLs correctly)
- Builds a synthetic master M3U8 from the format list instead of proxying Google's manifests
- Proxies individual HLS sub-playlists and segments through our server
- Works for both live streams and VOD
"""
import os
import re
import asyncio
import subprocess
import tempfile
import httpx
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="YT-M3U8 API", description="YouTube → proxied HLS streams", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.environ.get("API_KEY", "")
_default_cookies_dir = Path(__file__).parent / "data"
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", str(_default_cookies_dir)))
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

YT_HEADERS = {
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
        "http_headers": YT_HEADERS,
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
    return msg


def proxy_url(base_request: Request, target_url: str) -> str:
    base = str(base_request.base_url).rstrip("/")
    return f"{base}/proxy?url={quote(target_url, safe='')}"


def rewrite_m3u8(content: str, base_url: str, request: Request) -> str:
    """Rewrite all URLs in an M3U8 to go through our /proxy endpoint."""
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue

        # Rewrite URI="..." attributes in tags
        def replace_uri(m):
            uri = m.group(1)
            abs_url = urljoin(base_url, uri)
            return f'URI="{proxy_url(request, abs_url)}"'

        rewritten = re.sub(r'URI="([^"]+)"', replace_uri, line)

        # Rewrite bare segment/playlist URLs (lines not starting with #)
        if not stripped.startswith("#"):
            abs_url = urljoin(base_url, stripped)
            rewritten = proxy_url(request, abs_url)

        out.append(rewritten)
    return "\n".join(out)


def fetch_m3u8_via_ytdlp(manifest_url: str) -> str:
    """
    Fetch an HLS manifest using yt-dlp's internal downloader so it handles
    all the auth headers and signed URL quirks correctly.
    """
    opts = {
        **get_ydl_opts(),
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        # Use yt-dlp's urllib opener which has all the right headers/cookies
        opener = ydl.urlopen(manifest_url)
        return opener.read().decode("utf-8")


def build_master_m3u8_from_formats(info: dict, request: Request) -> str:
    """
    Build a synthetic HLS master playlist from yt-dlp format list.
    Each quality level points to /api/subplaylist which serves that format's stream.
    This avoids needing to proxy Google's IP-locked manifest URLs at all.
    """
    formats = info.get("formats", [])
    video_id = info.get("id", "")

    # Separate formats with video
    video_formats = [
        f for f in formats
        if f.get("vcodec") not in ("none", None)
        and f.get("height")
        and f.get("url")
    ]

    if not video_formats:
        raise HTTPException(404, "No playable video formats found")

    # Deduplicate by height, pick highest tbr for each height
    by_height: dict = {}
    for f in video_formats:
        h = f["height"]
        if h not in by_height or (f.get("tbr") or 0) > (by_height[h].get("tbr") or 0):
            by_height[h] = f

    sorted_formats = sorted(by_height.values(), key=lambda x: x["height"], reverse=True)

    base = str(request.base_url).rstrip("/")

    master = "#EXTM3U\n#EXT-X-VERSION:3\n\n"
    for f in sorted_formats[:8]:  # cap at 8 quality levels
        h = f["height"]
        w = f.get("width") or int(h * 16 / 9)
        bw = int((f.get("tbr") or 1000) * 1000)
        fps = f.get("fps") or 30
        fmt_id = f.get("format_id", "")

        # Sub-playlist URL for this specific format
        sub_url = (
            f"{base}/api/subplaylist"
            f"?video_url={quote(info.get('webpage_url', ''), safe='')}"
            f"&format_id={quote(fmt_id, safe='')}"
        )

        master += f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},FRAME-RATE={fps},NAME="{h}p"\n'
        master += f"{sub_url}\n"

    return master


def build_single_m3u8(stream_url: str, duration: float, title: str, request: Request) -> str:
    """Build a single-segment VOD M3U8 with a proxied stream URL."""
    proxied = proxy_url(request, stream_url)
    return (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{int(duration)+1}\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{duration:.3f},{title}\n"
        f"{proxied}\n"
        "#EXT-X-ENDLIST\n"
    )


# ── Proxy endpoint ────────────────────────────────────────────────────────────

ALLOWED_PROXY_HOSTS = (
    "googlevideo.com",
    "manifest.googlevideo.com",
    "rr1---sn", "rr2---sn", "rr3---sn", "rr4---sn", "rr5---sn",
    "googleusercontent.com",
    "youtube.com",
    "ytimg.com",
)


@app.get("/proxy", include_in_schema=False)
async def proxy_endpoint(request: Request, url: str = Query(...)):
    """Reverse-proxy HLS segments and manifests with correct YouTube headers."""
    target = unquote(url)
    host = urlparse(target).netloc

    if not any(allowed in host for allowed in ALLOWED_PROXY_HOSTS):
        raise HTTPException(403, f"Proxy not allowed for: {host}")

    # Detect if this is an M3U8 (manifest/playlist)
    # CRITICAL: seg.ts URLs contain "index.m3u8" mid-path, e.g.:
    #   /videoplayback/.../playlist/index.m3u8/sq/6781535/.../seg.ts  <- TS segment!
    # So we must check the FINAL path component, not substring match.
    parsed_target = urlparse(target)
    path_lower = parsed_target.path.lower()
    # "/sq/" in path = numbered segment chunk, never a manifest
    is_segment = "/sq/" in path_lower or path_lower.endswith(".ts") or path_lower.endswith(".aac") or path_lower.endswith(".m4s")
    is_m3u8 = not is_segment and (
        path_lower.endswith(".m3u8")
        or (path_lower.endswith("index.m3u8"))
        or ("/hls_playlist/" in path_lower and not is_segment)
        or ("/hls_manifest/" in path_lower and not is_segment)
    )

    if is_m3u8:
        # Fetch via yt-dlp's opener (handles auth correctly)
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, fetch_m3u8_via_ytdlp, target)
        except Exception as e:
            raise HTTPException(502, f"Failed to fetch manifest: {e}")
        rewritten = rewrite_m3u8(content, target, request)
        return PlainTextResponse(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    # Binary segment — fetch via yt-dlp opener (handles IP-signed URLs correctly)
    # httpx gets 403 because Google segments are IP+session signed; yt-dlp sends the right tokens
    path_end = path_lower.split("?")[0]
    if path_end.endswith(".ts") or "/seg.ts" in path_lower or "/sq/" in path_lower:
        ct = "video/mp2t"
    elif path_end.endswith(".m4s") or path_end.endswith(".mp4"):
        ct = "video/mp4"
    elif path_end.endswith(".aac"):
        ct = "audio/aac"
    else:
        ct = "application/octet-stream"

    def fetch_segment_bytes(url: str) -> bytes:
        """Fetch a segment synchronously via yt-dlp urlopen (handles auth)."""
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            resp = ydl.urlopen(url)
            return resp.read()

    async def stream_segment():
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, fetch_segment_bytes, target)
        except Exception as e:
            raise HTTPException(502, f"Segment fetch failed: {e}")
        yield data

    return StreamingResponse(
        stream_segment(),
        media_type=ct,
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


# ── Sub-playlist endpoint (per-quality HLS playlist) ─────────────────────────

@app.get("/api/subplaylist", response_class=PlainTextResponse, include_in_schema=False)
async def subplaylist(
    request: Request,
    video_url: str = Query(...),
    format_id: str = Query(...),
):
    """
    Returns a per-quality HLS sub-playlist for use in a master playlist.
    Re-extracts the stream URL fresh (handles expiry).
    """
    try:
        opts = {**get_ydl_opts(format_id), "format": format_id}
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None,
            lambda: extract_info(unquote(video_url), format_id)
        )
    except Exception as e:
        raise HTTPException(400, str(e))

    formats = info.get("formats", [])
    matched = [f for f in formats if f.get("format_id") == format_id and f.get("url")]
    if not matched:
        # Fall back to best
        matched = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
        if not matched:
            raise HTTPException(404, "Format not found")
        matched.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)

    chosen = matched[0]
    stream_url = chosen["url"]
    is_live = info.get("is_live") or info.get("live_status") == "is_live"

    # If this format has its own HLS manifest, proxy that
    if chosen.get("protocol") in ("m3u8", "m3u8_native") and chosen.get("url"):
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, fetch_m3u8_via_ytdlp, stream_url)
            rewritten = rewrite_m3u8(content, stream_url, request)
            return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl",
                                     headers={"Cache-Control": "no-cache"})
        except Exception:
            pass

    # VOD: build single-segment playlist
    dur = info.get("duration", 0) or 0
    title = info.get("title", "video")
    playlist = build_single_m3u8(stream_url, dur, title, request)
    return PlainTextResponse(playlist, media_type="application/vnd.apple.mpegurl",
                             headers={"Cache-Control": "no-cache"})


# ── Cookie management ─────────────────────────────────────────────────────────

@app.post("/api/cookies", summary="Upload cookies.txt", tags=["Setup"],
    description="Upload Netscape cookies.txt from YouTube. Fixes bot-detection errors.")
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
                "fix": "POST cookies.txt to /api/cookies"}
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
    return p.read_text() if p.exists() else "<h1>YT-M3U8 API v4</h1><a href='/docs'>Docs</a>"


@app.get("/api/m3u8", response_class=PlainTextResponse,
         summary="Get M3U8 master playlist", tags=["Core"])
async def get_m3u8(
    request: Request,
    url: str = Query(..., description="YouTube video URL", example="https://youtube.com/watch?v=dQw4w9WgXcQ"),
    quality: str = Query("best", description="best | worst | 1080 | 720 | 480 | 360 | 'master' for all qualities"),
):
    """
    Returns a proxied M3U8 playlist.
    - Use quality=master to get a master playlist with all quality levels.
    - All traffic is proxied through this server (CORS + referrer safe).
    - Works in browsers, VLC, mpv, ffmpeg, HLS.js.
    """
    check_key(request)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: extract_info(url))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    is_live = info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")

    if quality == "master" or is_live:
        # Build a synthetic master playlist — each quality hits /api/subplaylist
        master = build_master_m3u8_from_formats(info, request)
        return PlainTextResponse(
            master,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    # Single quality: find the best matching format
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

    # If this format is already an HLS stream, route through subplaylist
    if chosen.get("protocol") in ("m3u8", "m3u8_native"):
        base = str(request.base_url).rstrip("/")
        sub_url = (
            f"{base}/api/subplaylist"
            f"?video_url={quote(url, safe='')}"
            f"&format_id={quote(chosen.get('format_id', ''), safe='')}"
        )
        # Return a single-entry master pointing to subplaylist
        h = chosen.get("height", 0) or 0
        w = chosen.get("width") or int(h * 16/9)
        bw = int((chosen.get("tbr") or 1000) * 1000)
        master = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h}\n'
            f"{sub_url}\n"
        )
        return PlainTextResponse(master, media_type="application/vnd.apple.mpegurl",
                                 headers={"Cache-Control": "no-cache"})

    # VOD MP4: single-segment proxied playlist
    dur = info.get("duration", 0) or 0
    title = info.get("title", "video")
    playlist = build_single_m3u8(chosen["url"], dur, title, request)
    return PlainTextResponse(
        playlist,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*",
                 "Content-Disposition": 'inline; filename="playlist.m3u8"'},
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
                "m3u8_url": f"{request.base_url}api/m3u8?url={quote(url, safe='')}&quality={h}",
            })
    formats.sort(key=lambda x: x["height"], reverse=True)
    return {
        "title": info.get("title"),
        "is_live": is_live,
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "master_m3u8_url": f"{request.base_url}api/m3u8?url={quote(url, safe='')}&quality=master",
        "available_qualities": formats,
    }


@app.get("/api/stream-url", summary="Raw stream URL (no proxy)", tags=["Core"])
async def get_stream_url(request: Request, url: str = Query(...), quality: str = Query("best")):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    vf = [f for f in info.get("formats", []) if f.get("vcodec") not in ("none", None) and f.get("url")]
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
    c = vf[0]
    return {"title": info.get("title"), "quality": f"{c.get('height','?')}p",
            "ext": c.get("ext"), "url": c["url"],
            "note": "Direct URL expires ~6h. Use /api/m3u8 for stable proxied stream."}


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
