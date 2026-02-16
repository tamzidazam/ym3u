import os
import tempfile
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_cookies_path():
    cookie_content = os.environ.get("YOUTUBE_COOKIES")
    if not cookie_content:
        return None
    tfile = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt')
    tfile.write(cookie_content)
    tfile.close()
    return tfile.name

def extract_stream(url, client_type, cookie_path):
    """
    Tries to get a stream with a specific client disguise (ios, android, tv, etc.)
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_path,
        'extractor_args': {
            'youtube': {
                'player_client': [client_type]
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # 1. Prioritize HLS (m3u8)
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('protocol') == 'm3u8_native':
                        return f['url'], "m3u8"
            
            # 2. Check strict extensions
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('url', '').endswith('.m3u8'):
                        return f['url'], "m3u8"
            
            # 3. If no m3u8, return DASH (mpd) as fallback
            # (Better to have a working video than nothing)
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('protocol') == 'https' and f.get('ext') == 'mp4':
                        return f['url'], "mp4"

            raise ValueError("No formats found")
            
    except Exception as e:
        return None, str(e)

@app.get("/api/stream")
@app.get("/stream")
async def get_stream_url(url: str = Query(..., description="YouTube Video URL")):
    cookie_path = get_cookies_path()
    
    # === STRATEGY: TRY 3 DIFFERENT DISGUISES ===
    
    # Attempt 1: iOS (Best for m3u8, but often blocked on servers)
    stream_url, format_type = extract_stream(url, 'ios', cookie_path)
    if stream_url:
        if cookie_path: os.unlink(cookie_path)
        return {"stream_url": stream_url, "format": format_type, "client_used": "ios"}
        
    # Attempt 2: Android TV (Good for m3u8, often less blocked)
    stream_url, format_type = extract_stream(url, 'tv', cookie_path)
    if stream_url:
        if cookie_path: os.unlink(cookie_path)
        return {"stream_url": stream_url, "format": format_type, "client_used": "tv"}

    # Attempt 3: Android (Very reliable, but usually returns DASH/mp4, not m3u8)
    stream_url, format_type = extract_stream(url, 'android', cookie_path)
    if stream_url:
        if cookie_path: os.unlink(cookie_path)
        return {
            "stream_url": stream_url, 
            "format": format_type, 
            "client_used": "android",
            "warning": "m3u8 unavailable, returned fallback format"
        }

    # Cleanup
    if cookie_path and os.path.exists(cookie_path):
        os.unlink(cookie_path)

    raise HTTPException(status_code=500, detail="Failed to bypass YouTube server blocks. Try adding Cookies or a Proxy.")
