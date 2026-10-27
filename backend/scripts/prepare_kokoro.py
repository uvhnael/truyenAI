"""Chuẩn bị model Kokoro-Vietnamese (tải weight + voicepack mặc định về HF cache).

Không bắt buộc: engine tự tải từ HuggingFace khi tổng hợp lần đầu. Dùng lệnh này
để tải sẵn và xác nhận môi trường TTS hoạt động. Chạy: uv run python scripts/prepare_kokoro.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from kokoro_vietnamese import VOICES, KokoroVietnamese


def main() -> int:
    voice = settings.tts_voice if settings.tts_voice in VOICES else "diem_trinh"
    print(f"Tải model + voicepack giọng '{voice}' ({VOICES[voice]['label']}) ...", flush=True)
    KokoroVietnamese(voice=voice, device=settings.tts_device)
    print("Kokoro sẵn sàng.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())