"""Verify real ZeroTTS using an existing finished chapter, no LLM requests."""
import json
import os
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("API_URL", "http://localhost:18080")
PID = os.environ.get("AUDIO_PROJECT_ID", "d632ceaa-177a-493f-bcc7-39cfa1e4051b")
URL = f"{BASE}/api/projects/{PID}/chapters/1/audio"


def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


print(get(BASE + "/api/health"), flush=True)
st = get(URL + "/status")
if st["audio_status"] not in ("queued", "processing"):
    with urllib.request.urlopen(urllib.request.Request(URL + "?voice=maichi", data=b"", method="POST"), timeout=15) as r:
        print("Enqueued:", r.status, flush=True)
deadline = time.monotonic() + 240
previous = None
while time.monotonic() < deadline:
    st = get(URL + "/status")
    if st != previous:
        print(st, flush=True)
        previous = st
    if st["audio_status"] in ("ready", "failed", "stale"):
        break
    time.sleep(2)
assert st["audio_status"] == "ready", st
with urllib.request.urlopen(URL, timeout=30) as response:
    data = response.read()
    assert response.headers["content-type"] == "audio/mpeg"
assert len(data) > 1000
path = Path(__file__).resolve().parents[1] / ".artifacts" / "zerotts-chapter.mp3"
path.parent.mkdir(exist_ok=True)
path.write_bytes(data)
request = urllib.request.Request(URL, headers={"Range": "bytes=0-99"})
with urllib.request.urlopen(request) as r:
    assert r.status == 206 and len(r.read()) == 100
report = {"project_id": PID, "bytes": len(data), "status": st, "file": str(path), "range": "206"}
path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False), flush=True)
