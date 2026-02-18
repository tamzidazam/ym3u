"""
YT-M3U8 API v5

KEY INSIGHT: Google's segment URLs are session-signed — they only work during the 
yt-dlp extraction session. You cannot embed them in a playlist for later fetching.

SOLUTION: 
- /api/subplaylist fetches the live HLS manifest fresh via yt-dlp, then rewrites
  ALL segment URLs to point to /api/seg?video_url=...&sq=SEQUENCE_NUMBER
- /api/seg re-extracts the video info fresh each time, finds the segment by sequence
  number, and downloads it right then — within the same yt-dlp session
- This way segment URLs never expire because we never store them; we always fetch fresh
"""
import os
import re
import asyncio
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote, parse_qs
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="YT-M3U8 API", description="YouTube → HLS streams", version="5.0.0")
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
        return "YouTube bot-detection. Upload cookies via POST /api/cookies."
    if "age" in msg.lower():
        return "Age-restricted — upload YouTube cookies via POST /api/cookies."
    if "Private" in msg:
        return "Private video."
    if "members" in msg.lower():
        return "Members-only video."
    return msg


def seg_proxy_url(base_request: Request, video_url: str, format_id: str, sq: str) -> str:
    """Build a URL for /api/seg that fetches a segment by sequence number."""
    base = str(base_request.base_url).rstrip("/")
    return (
        f"{base}/api/seg"
        f"?video_url={quote(video_url, safe='')}"
        f"&format_id={quote(format_id, safe='')}"
        f"&sq={sq}"
    )


def rewrite_subplaylist(content: str, video_url: str, format_id: str, request: Request) -> str:
    """
    Rewrite an HLS sub-playlist so segment URLs become /api/seg?...&sq=N calls.
    The sequence number (sq=N) is extracted from each segment URL.
    This way segments are always fetched fresh — no stored session-bound URLs.
    """
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue

        # Extract sequence number from segment URL: /sq/NNNN/
        sq_match = re.search(r"/sq/(\d+)/", stripped)
        if sq_match:
            sq = sq_match.group(1)
            out.append(seg_proxy_url(request, video_url, format_id, sq))
        else:
            # No sq number — keep URL as-is (shouldn't happen for live streams)
            out.append(line)
    return "\n".join(out)


def fetch_manifest_for_format(video_url: str, format_id: str) -> tuple[str, str]:
    """
    Extract fresh info and return (manifest_content, manifest_url) for a specific format.
    Uses yt-dlp to both get the manifest URL and fetch its content.
    """
    with yt_dlp.YoutubeDL(get_ydl_opts(format_id)) as ydl:
        info = ydl.extract_info(video_url, download=False)
        formats = info.get("formats", [])

        # Find matching format
        matched = [f for f in formats if f.get("format_id") == format_id and f.get("url")]
        if not matched:
            matched = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
            if not matched:
                raise ValueError("No matching format found")
            matched.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)

        chosen = matched[0]
        manifest_url = chosen["url"]
        protocol = chosen.get("protocol", "")

        # If it's an HLS format, fetch the manifest content
        if protocol in ("m3u8", "m3u8_native"):
            resp = ydl.urlopen(manifest_url)
            content = resp.read().decode("utf-8")
            return content, manifest_url

        # VOD: build a simple playlist from the direct URL
        dur = info.get("duration", 0) or 0
        title = info.get("title", "video")
        playlist = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f"#EXT-X-TARGETDURATION:{int(dur)+1}\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            f"#EXTINF:{dur:.3f},{title}\n"
            f"{manifest_url}\n"
            "#EXT-X-ENDLIST\n"
        )
        return playlist, manifest_url


def fetch_segment_by_sq(video_url: str, format_id: str, sq: int) -> bytes:
    """
    Re-extract fresh URLs and fetch segment by sequence number within a SINGLE yt-dlp session.
    This avoids session expiry by doing extraction and download atomically.
    """
    with yt_dlp.YoutubeDL(get_ydl_opts(format_id)) as ydl:
        info = ydl.extract_info(video_url, download=False)
        formats = info.get("formats", [])

        matched = [f for f in formats if f.get("format_id") == format_id and f.get("url")]
        if not matched:
            matched = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
            if not matched:
                raise ValueError("No matching format found")
            matched.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)

        chosen = matched[0]
        manifest_url = chosen["url"]

        if chosen.get("protocol") not in ("m3u8", "m3u8_native"):
            # VOD — just download the whole stream URL
            resp = ydl.urlopen(manifest_url)
            return resp.read()

        # Fetch the manifest to find the correct segment URL for this sq number
        resp = ydl.urlopen(manifest_url)
        manifest = resp.read().decode("utf-8")

        # Find segment URL with matching sq number
        seg_url = None
        for line in manifest.splitlines():
            if f"/sq/{sq}/" in line and not line.startswith("#"):
                seg_url = urljoin(manifest_url, line.strip()) if not line.startswith("http") else line.strip()
                break

        if not seg_url:
            # Try building it from the manifest URL pattern
            # Manifest URL: .../playlist/index.m3u8/sq/PREV/... -> replace sq number
            seg_url = re.sub(r"/sq/\d+/", f"/sq/{sq}/", manifest_url)
            # Remove /playlist/index.m3u8 prefix parts if present
            if "/playlist/index.m3u8" in seg_url:
                seg_url = re.sub(r"/playlist/index\.m3u8/sq/\d+/(.*)", f"/sq/{sq}/\\1", seg_url)

        # Fetch the segment within the same session
        seg_resp = ydl.urlopen(seg_url)
        return seg_resp.read()


def build_master_m3u8(info: dict, request: Request) -> str:
    """Build a synthetic HLS master playlist from yt-dlp format list."""
    formats = info.get("formats", [])
    video_formats = [
        f for f in formats
        if f.get("vcodec") not in ("none", None) and f.get("height") and f.get("url")
    ]
    if not video_formats:
        raise HTTPException(404, "No playable video formats found")

    by_height: dict = {}
    for f in video_formats:
        h = f["height"]
        if h not in by_height or (f.get("tbr") or 0) > (by_height[h].get("tbr") or 0):
            by_height[h] = f

    sorted_formats = sorted(by_height.values(), key=lambda x: x["height"], reverse=True)
    base = str(request.base_url).rstrip("/")
    video_url = info.get("webpage_url", "")

    master = "#EXTM3U\n#EXT-X-VERSION:3\n\n"
    for f in sorted_formats[:8]:
        h = f["height"]
        w = f.get("width") or int(h * 16 / 9)
        bw = int((f.get("tbr") or 1000) * 1000)
        fps = f.get("fps") or 30
        fmt_id = f.get("format_id", "")

        sub_url = (
            f"{base}/api/subplaylist"
            f"?video_url={quote(video_url, safe='')}"
            f"&format_id={quote(fmt_id, safe='')}"
        )
        master += f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},FRAME-RATE={fps},NAME="{h}p"\n'
        master += f"{sub_url}\n"

    return master


# ── Segment endpoint — the key piece ─────────────────────────────────────────

@app.get("/api/seg", include_in_schema=False)
async def get_segment(
    request: Request,
    video_url: str = Query(...),
    format_id: str = Query(...),
    sq: int = Query(...),
):
    """
    Fetch a specific HLS segment by sequence number.
    Re-extracts fresh YouTube URLs each time — avoids session expiry completely.
    """
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None,
            fetch_segment_by_sq,
            unquote(video_url),
            format_id,
            sq,
        )
    except Exception as e:
        raise HTTPException(502, f"Segment fetch failed: {e}")

    return Response(
        content=data,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


# ── Sub-playlist endpoint ─────────────────────────────────────────────────────

@app.get("/api/subplaylist", response_class=PlainTextResponse, include_in_schema=False)
async def subplaylist(
    request: Request,
    video_url: str = Query(...),
    format_id: str = Query(...),
):
    """
    Fetches fresh HLS manifest and rewrites segment URLs to /api/seg calls.
    Called by the player for each quality level in the master playlist.
    """
    loop = asyncio.get_event_loop()
    try:
        content, manifest_url = await loop.run_in_executor(
            None,
            fetch_manifest_for_format,
            unquote(video_url),
            format_id,
        )
    except Exception as e:
        raise HTTPException(400, str(e))

    rewritten = rewrite_subplaylist(content, unquote(video_url), format_id, request)
    return PlainTextResponse(
        rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


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
    return {"status": "ok", "message": "No cookies loaded"}


# ── Core API ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    p = Path("static/index.html")
    return p.read_text() if p.exists() else "<h1>YT-M3U8 API v5</h1><a href='/docs'>Docs</a>"


@app.get("/api/m3u8", response_class=PlainTextResponse,
         summary="Get M3U8 master playlist", tags=["Core"])
async def get_m3u8(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
    quality: str = Query("best", description="best | worst | 1080 | 720 | 480 | 360 | master"),
):
    """
    Returns a proxied M3U8 master playlist.
    All segment fetching goes through /api/seg which re-extracts fresh URLs on every request.
    Works in VLC, mpv, ffmpeg. Browsers need HLS.js.
    """
    check_key(request)
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, extract_info, url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    is_live = info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")

    if quality == "master" or is_live:
        master = build_master_m3u8(info, request)
        return PlainTextResponse(master, media_type="application/vnd.apple.mpegurl",
                                 headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

    # Single quality
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
    base = str(request.base_url).rstrip("/")
    fmt_id = chosen.get("format_id", "")
    video_url = info.get("webpage_url", url)

    # Return a single-entry master pointing to subplaylist
    h = chosen.get("height", 0) or 0
    w = chosen.get("width") or int(h * 16 / 9)
    bw = int((chosen.get("tbr") or 1000) * 1000)
    sub_url = (
        f"{base}/api/subplaylist"
        f"?video_url={quote(video_url, safe='')}"
        f"&format_id={quote(fmt_id, safe='')}"
    )
    master = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h}\n'
        f"{sub_url}\n"
    )
    return PlainTextResponse(master, media_type="application/vnd.apple.mpegurl",
                             headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


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
    }


@app.get("/api/stream-url", summary="Raw stream URL", tags=["Core"])
async def get_stream_url(request: Request, url: str = Query(...), quality: str = Query("best")):
    """Direct signed URL — expires ~6h. Use /api/m3u8 for stable streams."""
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
