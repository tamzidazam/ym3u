import requests
import time

# A mix of Invidious and Piped instances to test
INSTANCES = [
    # --- Invidious ---
    "https://inv.tux.pizza",
    "https://vid.puffyan.us",
    "https://invidious.jing.rocks",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://yt.artemislena.eu",
    "https://invidious.projectsegfau.lt",
    "https://invidious.drgns.space",
    "https://invidious.privacy.com.de",
    
    # --- Piped ---
    "https://pipedapi.kavin.rocks",
    "https://api.piped.ot.ax",
    "https://piped-api.garudalinux.org",
    "https://pipedapi.drgns.space",
    "https://api.piped.privacy.com.de"
]

# A safe, short video ID to test (Rick Astley - Never Gonna Give You Up)
TEST_VIDEO_ID = "dQw4w9WgXcQ"

print(f"{'INSTANCE':<40} | {'STATUS':<10} | {'TIME':<10} | {'TYPE'}")
print("-" * 80)

good_instances = []

for url in INSTANCES:
    start = time.time()
    try:
        # Determine if it's Piped or Invidious based on URL structure
        if "piped" in url:
            api_url = f"{url}/streams/{TEST_VIDEO_ID}"
            instance_type = "Piped"
        else:
            api_url = f"{url}/api/v1/videos/{TEST_VIDEO_ID}"
            instance_type = "Invidious"

        # Set a strict 3-second timeout
        response = requests.get(api_url, timeout=3)
        duration = round(time.time() - start, 2)

        if response.status_code == 200:
            data = response.json()
            # Check if it actually gave us a stream link
            has_stream = False
            if instance_type == "Invidious" and ('hlsUrl' in data or 'formatStreams' in data):
                has_stream = True
            elif instance_type == "Piped" and ('hls' in data or 'videoStreams' in data):
                has_stream = True
            
            if has_stream:
                print(f"{url:<40} | \033[92mPASS\033[0m       | {duration}s      | {instance_type}")
                good_instances.append(url)
            else:
                print(f"{url:<40} | \033[93mNO STREAM\033[0m  | {duration}s      | {instance_type}")
        else:
            print(f"{url:<40} | \033[91mFAIL ({response.status_code})\033[0m | {duration}s      | {instance_type}")

    except Exception as e:
        print(f"{url:<40} | \033[91mDOWN\033[0m       | --          | {instance_type}")

print("\n" + "="*80)
print("UPDATED WORKING LIST (Copy this into your Worker):")
print("="*80)
print("const instances = [")
for url in good_instances:
    print(f'  "{url}",')
print("];")
