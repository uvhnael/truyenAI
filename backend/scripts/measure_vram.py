#!/usr/bin/env python
"""Single-run synthesis + in-process GPU VRAM peak sampling (nvidia-smi poll)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

# Vendored package + app live at the backend root, not alongside this script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

TEXT = (
    "Bé Mai bước chậm rãi dọc con dốc đá, hai tay ôm chặt chiếc chìa khóa nhỏ. "
    "Dưới gốc thông già, gió thổi qua tán lá, nghe như lời thì thầm của bà ngoại năm nào. "
    "Em ngồi xuống bậc đá lạnh, nhớ những buổi chiều cùng bà nướng bánh tráng bên bếp lửa, "
    "nhớ tiếng cười giòn tan giữa màn sương mù trắng đục của thành phố Đà Lạt. "
    "Chiếc chìa khóa trong tay bỗng ấm dần lên, như thể đang gọi em bước vào một câu chuyện "
    "chưa ai kể hết. Xung quanh, khu vườn quen thuộc bỗng chốc chìm vào bóng chiều, "
    "chỉ còn tiếng chim gọi bầy về tổ vang vọng giữa những gốc thông cao vút."
)

OUT = Path(__file__).resolve().parent.parent / ".artifacts" / "benchmark"


def sample_gpu_mem(stop: threading.Event) -> list[int]:
    seen: list[int] = []
    while not stop.is_set():
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2).stdout.strip()
            seen.append(int(out.splitlines()[0]))
        except Exception:
            pass
        stop.wait(0.03)
    return seen


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--engine", required=True, choices=["kokoro", "engine"])
    p.add_argument("--device", required=True, choices=["cuda"])
    args = p.parse_args()

    stop = threading.Event()
    results: list[int] = []
    def sampler():
        results.extend(sample_gpu_mem(stop))
    t = threading.Thread(target=sampler, daemon=True)
    t.start()

    OUT.mkdir(parents=True, exist_ok=True)
    wav = OUT / f"{args.engine}-{args.device}.vram.wav"
    t0 = time.perf_counter()
    if args.engine == "kokoro":
        from kokoro_vietnamese import SAMPLE_RATE, KokoroVietnamese
        tts = KokoroVietnamese(voice="diem_trinh", device=args.device)
        audio, _ = tts.synthesize(TEXT)
        sf.write(wav, audio, SAMPLE_RATE)
        audio_s = len(audio) / SAMPLE_RATE
    else:
        import app.agent.tts as ttm
        eng = ttm.TTSEngine(device=args.device)
        eng._load()
        audio_s = __import__("asyncio").run(eng.synthesize(TEXT, "diem_trinh", str(wav)))
    wall = time.perf_counter() - t0
    stop.set()
    t.join(timeout=5)

    peak = max(results) if results else 0
    print(f"{args.engine}/{args.device}: wall={wall:.3f}s audio={audio_s:.2f}s rtf={wall/audio_s:.4f} peak_vram_MiB={peak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())