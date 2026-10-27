"""Verify E2E video pipeline: POST render cho chương đã có audio → chờ → tải MP4 → ffprobe.

Chạy: python3 scripts/verify_video.py  (env: API_URL, VIDEO_PROJECT_ID, VIDEO_CHAPTER)
Không gọi LLM, không tốn phí; render tốn CPU (đo được ~27s cho 115s audio).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("API_URL", "http://localhost:18080")
PID = os.environ.get("VIDEO_PROJECT_ID", "d632ceaa-177a-493f-bcc7-39cfa1e4051b")
N = os.environ.get("VIDEO_CHAPTER", "1")


def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def field(state, *names):
    for name in names:
        if name in state:
            return state[name]
    return None


n = int(N)
print(get(BASE + "/api/health"), flush=True)
audio = get(f"{BASE}/api/projects/{PID}/chapters/{n}/audio/status")
assert audio["audio_status"] == "ready", f"cần audio ready trước khi render video: {audio}"

request = urllib.request.Request(
    f"{BASE}/api/projects/{PID}/chapters/{n}/video",
    data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=15) as response:
    print("POST video:", response.status, response.read().decode()[:200], flush=True)

deadline = time.monotonic() + 600
state, previous = {}, None
while time.monotonic() < deadline:
    state = get(f"{BASE}/api/projects/{PID}/chapters/{n}/video/status")
    if state != previous:
        print(state, flush=True)
        previous = state
    if field(state, "video_status", "status") in ("ready", "failed", "stale"):
        break
    time.sleep(3)
assert field(state, "video_status", "status") == "ready", state

url = f"{BASE}/api/projects/{PID}/chapters/{n}/video"
with urllib.request.urlopen(url, timeout=300) as response:
    data = response.read()
    assert response.headers["content-type"] == "video/mp4", response.headers["content-type"]
with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-99"}), timeout=30) as response:
    assert response.status == 206 and len(response.read()) == 100, "Range request failed"

path = Path(__file__).resolve().parents[1] / ".artifacts" / "chapter-video.mp4"
path.parent.mkdir(exist_ok=True)
path.write_bytes(data)
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
     "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
assert probe.returncode == 0, probe.stderr
info = json.loads(probe.stdout)
streams = {s["codec_type"]: s for s in info["streams"]}
assert streams["video"]["width"] == 1920 and streams["video"]["height"] == 1080, streams
print(json.dumps({"bytes": len(data), "range": "206", "duration": info["format"]["duration"],
                  "video": {k: streams["video"][k] for k in ("codec_name", "width", "height")},
                  "audio": streams["audio"]["codec_name"], "file": str(path)}, ensure_ascii=False), flush=True)
sys.exit(0)
