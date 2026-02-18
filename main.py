import os
import base64
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="YT-M3U8 API", description="Convert YouTube videos to M3U8 HLS streams", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY     = os.environ.get("API_KEY", "")
# Use a local writable directory — /app doesn't exist on Render free tier
_default_cookies_dir = Path(__file__).parent / "data"
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", str(_default_cookies_dir)))
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def find_cookies() -> str | None:
    candidates = [
        str(COOKIES_DIR / "cookies.txt"),
        os.environ.get("COOKIES_FILE", ""),
        "./cookies.txt",
        "./data/cookies.txt",
    ]
    for p in candidates:
        if p and Path(p).exists() and Path(p).stat().st_size > 0:
            return p
    return None


def get_ydl_opts(fmt="best") -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        # Spoof a real browser to reduce bot detection
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Use Android + web clients — Android client bypasses some restrictions
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]}
        },
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


def pick_format(formats, quality):
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
    return vf[0]


def build_m3u8(info, quality="best"):
    formats = info.get("formats", [])
    for f in formats:
        if f.get("protocol") in ("m3u8", "m3u8_native") and f.get("url"):
            return f["url"], "redirect"
    chosen = pick_format(formats, quality)
    dur = info.get("duration", 0) or 0
    title = info.get("title", "video")
    playlist = (
        f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:{int(dur)+1}\n"
        f"#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{dur:.3f},{title}\n{chosen['url']}\n#EXT-X-ENDLIST\n"
    )
    return playlist, "playlist"


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "Sign in to confirm" in msg or "bot" in msg.lower():
        return (
            "YouTube bot-detection triggered. "
            "Fix: export your browser cookies from youtube.com using the "
            "'Get cookies.txt LOCALLY' Chrome extension, then upload via POST /api/cookies. "
            "See /docs for details."
        )
    if "age" in msg.lower():
        return "Age-restricted video — upload YouTube cookies via POST /api/cookies."
    if "Private" in msg:
        return "Private video — needs cookies from an account with access."
    if "members" in msg.lower():
        return "Members-only video — needs cookies from a member account."
    return msg


# ── Cookie management ─────────────────────────────────────────────────────────

@app.post("/api/cookies", summary="Upload cookies.txt", tags=["Setup"],
    description=(
        "Upload a Netscape-format cookies.txt from your browser. "
        "**How to get it:** Install 'Get cookies.txt LOCALLY' in Chrome, "
        "log into YouTube, then click the extension and export. "
        "This fixes the 'Sign in to confirm you're not a bot' error."
    ))
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    check_key(request)
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    text = content.decode("utf-8", errors="replace")
    if "# Netscape HTTP Cookie File" not in text and "youtube" not in text.lower():
        raise HTTPException(400, "Doesn't look like a valid Netscape cookies.txt from YouTube.")
    dest = COOKIES_DIR / "cookies.txt"
    dest.write_bytes(content)
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    return {"status": "ok", "entries": len(lines), "has_youtube": "youtube.com" in text}


@app.get("/api/cookies/status", summary="Cookie status", tags=["Setup"])
async def cookies_status(request: Request):
    check_key(request)
    path = find_cookies()
    if not path:
        return {
            "cookies_loaded": False,
            "fix": "POST your cookies.txt to /api/cookies. "
                   "Export from YouTube using 'Get cookies.txt LOCALLY' Chrome extension.",
        }
    text = Path(path).read_text(errors="replace")
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    return {
        "cookies_loaded": True,
        "path": path,
        "entries": len(lines),
        "size_kb": Path(path).stat().st_size // 1024,
        "has_youtube_cookies": "youtube.com" in text,
    }


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
    return p.read_text() if p.exists() else "<h1>YT-M3U8 API v2</h1><a href='/docs'>Docs</a>"


@app.get("/api/m3u8", response_class=PlainTextResponse, summary="Get M3U8 playlist", tags=["Core"])
async def get_m3u8(
    request: Request,
    url: str = Query(..., description="YouTube video URL", example="https://youtube.com/watch?v=dQw4w9WgXcQ"),
    quality: str = Query("best", description="best | worst | 1080 | 720 | 480 | 360"),
):
    """Returns M3U8 playlist. Use directly in VLC, mpv, HLS.js, ffmpeg."""
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    result, mode = build_m3u8(info, quality)
    if mode == "redirect":
        return RedirectResponse(result, 302)
    return PlainTextResponse(result, media_type="application/vnd.apple.mpegurl",
                             headers={"Content-Disposition": 'inline; filename="playlist.m3u8"'})


@app.get("/api/formats", summary="List formats", tags=["Info"])
async def get_formats(request: Request, url: str = Query(...)):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
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
    return {"title": info.get("title"), "duration": info.get("duration"),
            "uploader": info.get("uploader"), "thumbnail": info.get("thumbnail"),
            "available_qualities": formats}


@app.get("/api/stream-url", summary="Raw stream URL", tags=["Core"])
async def get_stream_url(
    request: Request,
    url: str = Query(...),
    quality: str = Query("best"),
):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    chosen = pick_format(info.get("formats", []), quality)
    return {"title": info.get("title"), "quality": f"{chosen.get('height','?')}p",
            "ext": chosen.get("ext"), "url": chosen["url"],
            "note": "Signed URL expires ~6h. Re-call for fresh URL."}


@app.get("/api/info", summary="Full video metadata", tags=["Info"])
async def get_info(request: Request, url: str = Query(...)):
    check_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, friendly_error(e))
    formats = [{"format_id": f.get("format_id"), "ext": f.get("ext"), "height": f.get("height"),
                "width": f.get("width"), "fps": f.get("fps"), "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"), "tbr": f.get("tbr"), "protocol": f.get("protocol"),
                "url": f.get("url")} for f in info.get("formats", [])]
    return {"id": info.get("id"), "title": info.get("title"),
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
