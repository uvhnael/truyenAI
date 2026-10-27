"""Kokoro-Vietnamese bounded TTS; version fencing and atomic audio publication.

Engine wraps ``kokoro_vietnamese.KokoroVietnamese``. A single model instance is
cached per voice (voicepacks are tiny style embeddings); each voice change reloads
only the needed instance. Sampling rate is 24 kHz (Kokoro).
"""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from sqlalchemy import select

from kokoro_vietnamese import VOICES as _KOKORO_VOICES
from kokoro_vietnamese import KokoroVietnamese

from ..config import settings
from ..db import Session
from ..models import Chapter, Event

# id -> display label; the API serves these and validates requested voices.
VOICES = {voice: info["label"] for voice, info in _KOKORO_VOICES.items()}

# Kokoro model files that must exist under tts_model_dir when that override is set.
_DEFAULT_CONFIG_FILE = "config.json"
_DEFAULT_MODEL_FILE = "kokoro_vi.pth"
_MAX_CACHED_VOICES = 3


class TTSError(RuntimeError):
    pass


# Sentence enders. A segment is closed only after one of these (plus any
# surrounding closing quote/paren), so synthesis never starts mid-sentence.
_SENT_END = ".!?…"
_CLAUSE = ",;:،"
_SENT_CLOSERS = {'"', "'", "”", "’", ")", "]", "}"}


def _sentence_case_chars(sent: str) -> bool:
    return sent.strip() != ""


def _split_sentences(text: str) -> list[str]:
    """Split text at sentence-ending punctuation, keeping closing quotes with their sentence."""
    sentences = []
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in _SENT_END:
            j = i + 1
            while j < n and text[j] in _SENT_CLOSERS:
                buf.append(text[j])
                j += 1
            if ch == "…":
                k = j
                while k < n and text[k].isspace():
                    k += 1
                if k < n and text[k].islower():
                    i += 1
                    continue
            if j >= n or text[j].isspace() or text[j] in _CLAUSE:
                if _sentence_case_chars("".join(buf)):
                    sentences.append("".join(buf).strip())
                buf = []
                i = j
                continue
        i += 1
    if _sentence_case_chars("".join(buf)):
        sentences.append("".join(buf).strip())
    return sentences


def _preferred_cut(rest: str, limit: int) -> int:
    for sep in _CLAUSE:
        pos = rest.rfind(sep, 0, limit)
        if pos > 0:
            return pos + 1
    pos = rest.rfind(" ", 0, limit)
    if pos > 0:
        return pos + 1
    return limit


def split_text(text: str, limit: int) -> list[str]:
    """Segment at sentence boundaries so a reading never starts mid-sentence.

    Keeps every non-whitespace character. A sentence longer than `limit` is broken
    at a clause separator first, then a word boundary, then (only as a last resort)
    at the hard limit — never splitting a word when a boundary exists.
    """
    result = []
    current = ""
    for sent in _split_sentences(text or ""):
        if not sent:
            continue
        if current and len(current) + len(sent) + 1 <= limit:
            current += " " + sent
            continue
        if current:
            result.append(current)
        if len(sent) <= limit:
            current = sent
        else:
            rest = sent
            while len(rest) > limit:
                cut = _preferred_cut(rest, limit)
                result.append(rest[:cut].strip())
                rest = rest[cut:].strip()
            current = rest
    if current:
        result.append(current)
    return result


class TTSEngine:
    def __init__(self, model_dir=None, threads=None, device=None):
        self.model_dir = model_dir or settings.tts_model_dir or None
        self.device = device or settings.tts_device
        if self.device not in ("cpu", "cuda"):
            raise ValueError("TTS device must be cpu or cuda")
        self._cache: dict[str, Any] = {}  # voice -> KokoroVietnamese instance
        self._lock = asyncio.Lock()

    def _instance(self, voice: str) -> Any:
        cached = self._cache.get(voice)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {"voice": voice, "device": self.device}
        if self.model_dir:
            base = Path(self.model_dir).resolve()
            if not (base / _DEFAULT_CONFIG_FILE).is_file():
                raise TTSError("Thiếu model Kokoro; kiểm tra thư mục model.")
            kwargs["config_path"] = str(base / _DEFAULT_CONFIG_FILE)
            kwargs["model_path"] = str(base / _DEFAULT_MODEL_FILE)
        try:
            instance = KokoroVietnamese(**kwargs)
        except Exception as exc:
            raise TTSError(
                "Chưa tải được model Kokoro. Kiểm tra mạng/kết nối HuggingFace, "
                "hoặc đặt TTS_MODEL_DIR trỏ tới thư mục chứa model."
            ) from exc
        if len(self._cache) >= _MAX_CACHED_VOICES:
            # drop the oldest cached voice to bound VRAM on small GPUs
            self._cache.pop(next(iter(self._cache)))
        self._cache[voice] = instance
        return instance

    def _load(self) -> None:
        self._instance(settings.tts_voice or "diem_trinh")

    def _synth(self, text: str, voice: str, wav_path: str) -> float:
        from kokoro_vietnamese import SAMPLE_RATE

        instance = self._instance(voice)
        audio, _ = instance.synthesize(text)
        audio = np.asarray(audio)
        if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
            raise TTSError("Kokoro trả dữ liệu âm thanh không hợp lệ")
        sf.write(wav_path, audio, SAMPLE_RATE, subtype="PCM_16")
        return len(audio) / SAMPLE_RATE

    async def synthesize(self, text: str, voice: str, wav_path: str) -> float:
        if voice not in VOICES:
            raise TTSError("Giọng đọc không hợp lệ")

        async with self._lock:
            # Cancellation must not free the lock while a native thread is still
            # using torch. Wait for it before allowing another synthesis.
            task = asyncio.create_task(asyncio.to_thread(self._synth, text, voice, wav_path))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise


engine = TTSEngine()


def encode_mp3(wav: Path, mp3: Path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                    "-codec:a", "libmp3lame", "-qscale:a", "4", str(mp3)],
                   check=True, capture_output=True, timeout=120)
    if mp3.stat().st_size < 100:
        raise TTSError("File MP3 rỗng")


async def run_audio(project_id: str, n: int, generation: int):
    """Worker holds a global PG advisory lock; writes are chapter-version fenced."""
    async with Session() as s, s.begin():
        c = await s.scalar(select(Chapter).where(Chapter.project_id == project_id,
                          Chapter.chapter_number == n).with_for_update())
        if not c or c.audio_generation != generation or c.audio_status not in ("queued", "processing"):
            return
        version, voice, content = c.version, c.audio_voice, c.content
        if c.audio_version != version:
            c.audio_status = "stale"
            return
        c.audio_status, c.audio_error = "processing", None
        s.add(Event(project_id=project_id, type="audio_processing", payload={"chapter_number": n}))
    try:
        directory = Path(settings.audio_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tts-", dir=directory) as tmp:
            wav, mp3 = Path(tmp, "audio.wav"), Path(tmp, "audio.mp3")
            seconds = await engine.synthesize(content, voice, str(wav))
            await asyncio.to_thread(encode_mp3, wav, mp3)
            async with Session() as s, s.begin():
                c = await s.scalar(select(Chapter).where(Chapter.project_id == project_id,
                                  Chapter.chapter_number == n).with_for_update())
                if c.audio_generation != generation or c.version != version:
                    return
                filename = f"{c.id}-v{version}-g{generation}.mp3"
                os.replace(mp3, directory / filename)
                c.audio_status, c.audio_path = "ready", str(directory / filename)
                c.audio_seconds = round(seconds, 2)
                s.add(Event(project_id=project_id, type="audio_ready", payload={
                    "chapter_number": n, "seconds": round(seconds, 2), "voice": voice}))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, TTSError) else f"Tổng hợp thất bại ({type(exc).__name__}); kiểm tra model và thư mục audio."
        async with Session() as s, s.begin():
            c = await s.scalar(select(Chapter).where(Chapter.project_id == project_id,
                              Chapter.chapter_number == n).with_for_update())
            if c and c.audio_generation == generation:
                c.audio_status, c.audio_error = "failed", detail
                s.add(Event(project_id=project_id, type="audio_failed",
                            payload={"chapter_number": n, "error": detail}))