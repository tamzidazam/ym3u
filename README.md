# 🎬 YT-M3U8 API

Convert any YouTube video to an M3U8 HLS stream link. Works with VLC, mpv, ffmpeg, HLS.js, and any HLS-capable player.

## Features

- 🔗 **M3U8 playlist generation** from YouTube URLs
- 📺 **Quality selection** — best, worst, or specific height (720, 1080, etc.)
- 🔑 **Optional API key** authentication
- 🍪 **Cookie support** for age-restricted / member-only content
- 📖 **Swagger UI** at `/docs`
- 🌐 **Web UI** included at `/`

---

## 🚀 Deploy to Render (Recommended — Free)

1. **Fork or push this repo to GitHub**

2. Go to [render.com](https://render.com) → New → Web Service

3. Connect your GitHub repo

4. Set these settings:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`

5. Add environment variables (optional):
   - `API_KEY` — set to any secret string to require auth; leave empty for open access
   - `COOKIES_FILE` — path to your cookies.txt file (default: `/app/cookies.txt`)

6. (Optional) Add a **Disk** at mount path `/app` to upload your `cookies.txt`

7. Deploy! Your API will be live at `https://your-app.onrender.com`

---

## 🍪 Using Browser Cookies (for Age-restricted Videos)

YouTube sometimes requires authentication. Export your browser cookies using the **EditThisCookie** or **Get cookies.txt LOCALLY** browser extension:

1. Log into YouTube in your browser
2. Install "Get cookies.txt LOCALLY" extension
3. Go to youtube.com → Click the extension → Export `cookies.txt`
4. Upload `cookies.txt` to your Render disk at `/app/cookies.txt`
5. Set `COOKIES_FILE=/app/cookies.txt` in env vars

---

## 📡 API Reference

All endpoints accept an optional `?api_key=` query param or `X-API-Key` header if `API_KEY` is set.

### `GET /api/m3u8`
Returns M3U8 playlist content.

| Param | Default | Description |
|-------|---------|-------------|
| `url` | required | YouTube video URL |
| `quality` | `best` | `best`, `worst`, `1080`, `720`, `480`, `360` |

```bash
curl "https://your-app.onrender.com/api/m3u8?url=https://youtube.com/watch?v=dQw4w9WgXcQ&quality=720"
```

### `GET /api/formats`
Lists all available quality options.

```bash
curl "https://your-app.onrender.com/api/formats?url=VIDEO_URL"
```

### `GET /api/stream-url`
Returns raw signed stream URL (~6h expiry).

```bash
curl "https://your-app.onrender.com/api/stream-url?url=VIDEO_URL&quality=720"
```

### `GET /api/info`
Full video metadata + all formats.

```bash
curl "https://your-app.onrender.com/api/info?url=VIDEO_URL"
```

---

## 🖥️ Usage Examples

### VLC
```bash
vlc "https://your-app.onrender.com/api/m3u8?url=VIDEO_URL"
```

### mpv
```bash
mpv "https://your-app.onrender.com/api/m3u8?url=VIDEO_URL"
```

### ffmpeg (download)
```bash
ffmpeg -i "https://your-app.onrender.com/api/m3u8?url=VIDEO_URL" -c copy output.mp4
```

### Python
```python
import requests

BASE = "https://your-app.onrender.com"
VIDEO = "https://youtube.com/watch?v=dQw4w9WgXcQ"

# Get M3U8 content
m3u8 = requests.get(f"{BASE}/api/m3u8", params={"url": VIDEO, "quality": "720"}).text

# Get formats
formats = requests.get(f"{BASE}/api/formats", params={"url": VIDEO}).json()
print(formats["available_qualities"])
```

### JavaScript + HLS.js
```javascript
const m3u8Url = `https://your-app.onrender.com/api/m3u8?url=${encodeURIComponent(videoUrl)}&quality=720`;

const hls = new Hls();
hls.loadSource(m3u8Url);
hls.attachMedia(document.getElementById('video'));
```

---

## ⚠️ Notes

- YouTube signed URLs expire in ~6 hours. The M3U8 link itself is stable (re-fetches stream URL on each play)
- For best compatibility, use VLC or mpv since they handle all stream types
- Age-restricted videos require cookies
- Member-only content requires cookies from an account with membership

---

## 🏃 Run Locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# Visit http://localhost:8000
```
