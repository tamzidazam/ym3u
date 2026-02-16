import os
import tempfile
from fastapi import FastAPI, HTTPException, Query
import yt_dlp

app = FastAPI()

def get_cookies_path():
    """
    Writes the cookies from the env var to a temporary file 
    and returns the path.
    """
    cookie_content = os.environ.get("YOUTUBE_COOKIES")
    if not cookie_content:
        return None
    
    # Create a temporary file that closes automatically only when we want
    # Note: On Unix (Vercel/Render), we can read an open file, but safest is to close it first.
    tfile = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt')
    tfile.write(cookie_content)
    tfile.close()
    return tfile.name

def get_m3u8(video_url: str):
    cookie_path = get_cookies_path()
    
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        # 1. Use the temp cookie file
        'cookiefile': cookie_path, 
        # 2. Spoof a common user agent to look less like a bot
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        # 3. Use the Android client which is often more lenient
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            # Clean up the temp cookie file
            if cookie_path and os.path.exists(cookie_path):
                os.unlink(cookie_path)

            if 'formats' in info:
                for f in info['formats']:
                    if f.get('protocol') == 'm3u8_native':
                        return f['url']
                    if f.get('url', '').endswith('.m3u8'):
                        return f['url']
            
            if 'url' in info and info['url'].endswith('.m3u8'):
                return info['url']
                
            raise ValueError("No m3u8 stream found.")

    except Exception as e:
        # Clean up in case of error
        if cookie_path and os.path.exists(cookie_path):
            os.unlink(cookie_path)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stream")
async def get_stream_url(url: str = Query(..., description="YouTube Video URL")):
    return {
        "original_url": url,
        "stream_url": get_m3u8(url)
    }

