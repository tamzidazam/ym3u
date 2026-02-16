from fastapi import FastAPI, HTTPException, Query
import yt_dlp

app = FastAPI()

def get_m3u8(video_url: str):
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        # 'cookiefile': 'cookies.txt', # Uncomment if using cookies
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            # YouTube live streams usually offer an HLS manifest URL directly
            if 'formats' in info:
                # Look for the m3u8 format specifically
                for f in info['formats']:
                    if f.get('ext') == 'mp4' and f.get('protocol') == 'm3u8_native':
                        return f['url']
                    # Fallback for some live streams
                    if f.get('url', '').endswith('.m3u8'):
                        return f['url']
            
            # Direct extraction if available in top level
            if 'url' in info and info['url'].endswith('.m3u8'):
                return info['url']
                
            raise ValueError("No m3u8 stream found. Is this a live stream?")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stream")
async def get_stream_url(url: str = Query(..., description="YouTube Video URL")):
    """
    Takes a YouTube URL and returns the direct m3u8 link.
    """
    m3u8_url = get_m3u8(url)
    return {
        "original_url": url,
        "stream_url": m3u8_url
    }

# Health check
@app.get("/")
def read_root():
    return {"status": "Service is running", "platform": "FastAPI + yt-dlp"}
