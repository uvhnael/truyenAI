#!/usr/bin/env python
"""Synthesize a Vietnamese sample with Kokoro-Vietnamese voices.

Usage:
  uv run python scripts/test_kokoro.py --list
  uv run python scripts/test_kokoro.py --voice diem_trinh --device cpu
  uv run python scripts/test_kokoro.py --all --device cpu

Writes .wav (24000 Hz PCM) and .mp3 into `.artifacts/kokoro/`.
Model weights + voicepacks are downloaded on first use from
huggingface.co/contextboxai/Kokoro-Vietnamese to the HF cache.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Vendored Kokoro package + app live at the backend root, not alongside this script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

SAMPLE = (
    "Giữa một buổi chiều yên tĩnh, cô ấy kể lại câu chuyện "
    "bằng một giọng nói ấm áp và chậm rãi."
)

OUT = Path(__file__).resolve().parent.parent / ".artifacts" / "kokoro"


def encode_mp3(wav: Path, mp3: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-qscale:a", "4", str(mp3)],
        check=True, capture_output=True, timeout=120,
    )


def render(voice: str, device: str, text: str) -> float:
    from kokoro_vietnamese import SAMPLE_RATE, VOICES, KokoroVietnamese

    label = VOICES[voice]["label"]
    print(f"[*] loading KokoroVietnamese voice={voice!r} ({label}) device={device} ...", flush=True)
    t0 = time.time()
    tts = KokoroVietnamese(voice=voice, device=device)
    print(f"    model+voicepack loaded in {time.time() - t0:.1f}s", flush=True)

    t0 = time.time()
    audio, _phonemes = tts.synthesize(text)
    seconds = len(audio) / SAMPLE_RATE
    print(f"    synthesized {seconds:.2f}s of audio in {time.time() - t0:.1f}s (RTF ~{seconds / max(time.time() - t0, 1e-9):.2f})", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    wav = OUT / f"{voice}.wav"
    mp3 = OUT / f"{voice}.mp3"
    sf.write(wav, audio, SAMPLE_RATE, subtype="PCM_16")
    encode_mp3(wav, mp3)
    print(f"    wrote {wav.name} ({wav.stat().st_size / 1e6:.2f} MB) and {mp3.name} "
          f"({mp3.stat().st_size / 1e6:.2f} MB)", flush=True)
    return seconds


def main() -> int:
    from kokoro_vietnamese import VOICES

    p = argparse.ArgumentParser(description="Kokoro-Vietnamese voice test")
    p.add_argument("--voice", default="diem_trinh")
    p.add_argument("--text", default=SAMPLE)
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    p.add_argument("--list", action="store_true", help="list voices and exit")
    p.add_argument("--all", action="store_true", help="render every voice")
    args = p.parse_args()

    if args.list:
        for vid in VOICES:
            print(f"{vid:<12} {VOICES[vid]['label']}")
        return 0

    voices = sorted(VOICES) if args.all else [args.voice]
    print(f"Rendering {len(voices)} voice(s) to {OUT.resolve()}")

    total_start = time.time()
    for vid in voices:
        try:
            secs = render(vid, args.device, args.text)
            print(f"  = {vid}: {secs:.2f}s\n", flush=True)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  ! {vid}: FAILED: {type(exc).__name__}: {exc}\n", flush=True)
            if not args.all:
                return 1

    print(f"Done in {time.time() - total_start:.1f}s. Outputs in {OUT.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())