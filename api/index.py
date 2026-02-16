from fastapi import FastAPI, HTTPException, Query
import requests

app = FastAPI()

# List of public instances (we try a few in case one is down)
INSTANCES = [
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://invidious.nerdvpn.de"
]

@app.get("/api/stream")
@app.get("/stream")
def get_stream(url: str = Query(..., description="YouTube Video URL")):
    # Extract Video ID (e.g., dQw4w9WgXcQ)
    if "v=" in url:
        video_id = url.split("v=")[1].split("&")[0]
    elif "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0]
    else:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    # Try each instance until one works
    for instance in INSTANCES:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}"
            response = requests.get(api_url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                
                # Invidious returns 'formatStreams' (mp4) and 'hlsUrl' (m3u8)
                # We prioritize HLS (m3u8)
                if 'hlsUrl' in data:
                    return {
                        "stream_url": f"{instance}{data['hlsUrl']}",
                        "source": "invidious-hls",
                        "instance": instance
                    }
                
                # Fallback to standard MP4 streams if HLS is missing
                if 'formatStreams' in data:
                    # Get the highest quality video
                    best_stream = data['formatStreams'][-1]['url'] 
                    return {
                        "stream_url": best_stream,
                        "source": "invidious-mp4",
                        "instance": instance
                    }
        except:
            continue

    raise HTTPException(status_code=503, detail="All Invidious instances failed. YouTube is tough today.")
