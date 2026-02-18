import os
import base64
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(
    title="YT-M3U8 API",
    description="Convert YouTube videos to M3U8 HLS streams",
    version="1.0.0",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.environ.get("API_KEY", "")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/app/cookies.txt")


def get_ydl_opts(format_selector="best"):
    opts = {"quiet": True, "no_warnings": True, "format": format_selector}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def check_api_key(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def extract_info(url: str, format_selector="best"):
    with yt_dlp.YoutubeDL(get_ydl_opts(format_selector)) as ydl:
        return ydl.extract_info(url, download=False)


def pick_format(formats, quality):
    video_formats = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("url")]
    if not video_formats:
        raise HTTPException(status_code=404, detail="No playable formats found")
    if quality == "best":
        video_formats.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
    elif quality == "worst":
        video_formats.sort(key=lambda x: x.get("height", 0) or 0)
    else:
        try:
            t = int(quality)
            video_formats.sort(key=lambda x: abs((x.get("height", 0) or 0) - t))
        except ValueError:
            video_formats.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
    return video_formats[0]


def build_m3u8(info, quality="best"):
    formats = info.get("formats", [])
    # Try native HLS first
    for f in formats:
        if f.get("protocol") in ("m3u8", "m3u8_native") and f.get("url"):
            return f["url"], "redirect"
    chosen = pick_format(formats, quality)
    duration = info.get("duration", 0) or 0
    title = info.get("title", "video")
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{int(duration)+1}\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{duration:.3f},{title}\n"
        f"{chosen['url']}\n"
        "#EXT-X-ENDLIST\n"
    )
    return playlist, "playlist"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    if os.path.exists("static/index.html"):
        return open("static/index.html").read()
    return "<h1>YT-M3U8 API</h1><a href='/docs'>Docs</a>"


@app.get("/api/m3u8", response_class=PlainTextResponse,
         summary="Get M3U8 playlist", tags=["Core"])
async def get_m3u8(
    request: Request,
    url: str = Query(..., description="YouTube video URL", example="https://youtube.com/watch?v=dQw4w9WgXcQ"),
    quality: str = Query("best", description="best | worst | 1080 | 720 | 480 | 360"),
):
    """Returns M3U8 playlist content. Redirect to native HLS if available."""
    check_api_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    result, mode = build_m3u8(info, quality)
    if mode == "redirect":
        return RedirectResponse(result, 302)
    return PlainTextResponse(result, media_type="application/vnd.apple.mpegurl",
                             headers={"Content-Disposition": 'inline; filename="playlist.m3u8"'})


@app.get("/api/formats", summary="List formats", tags=["Info"])
async def get_formats(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
):
    """List all available video quality options."""
    check_api_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, detail=str(e))

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


@app.get("/api/stream-url", summary="Get raw stream URL", tags=["Core"])
async def get_stream_url(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
    quality: str = Query("best", description="best | worst | 720 | 1080 | etc."),
):
    """Returns signed direct stream URL (~6h expiry). Re-call to refresh."""
    check_api_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, detail=str(e))

    chosen = pick_format(info.get("formats", []), quality)
    return {"title": info.get("title"), "quality": f"{chosen.get('height','?')}p",
            "ext": chosen.get("ext"), "url": chosen["url"],
            "note": "Signed URL expires ~6h. Re-call for fresh URL."}


@app.get("/api/info", summary="Full video metadata", tags=["Info"])
async def get_info(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
):
    """Full video metadata including all format details."""
    check_api_key(request)
    try:
        info = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, detail=str(e))

    formats = [{"format_id": f.get("format_id"), "ext": f.get("ext"),
                "height": f.get("height"), "width": f.get("width"),
                "fps": f.get("fps"), "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"), "tbr": f.get("tbr"),
                "protocol": f.get("protocol"), "url": f.get("url")}
               for f in info.get("formats", [])]

    return {"id": info.get("id"), "title": info.get("title"),
            "description": (info.get("description") or "")[:500],
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"), "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"), "like_count": info.get("like_count"),
            "formats_count": len(formats), "formats": formats}


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "yt_dlp_version": yt_dlp.version.__version__}


if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
