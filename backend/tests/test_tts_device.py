"""Kokoro engine unit tests: device routing, voice caching, WAV output; no model/GPU/network.

The real KokoroVietnamese is replaced with a stub so tests never download weights
or touch torch CUDA. Behavior verified here: device validation, per-voice instance
caching, WAV writing from the returned audio, and the missing-model error.
"""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import app.agent.tts as tts
from app.agent.tts import VOICES, TTSEngine, TTSError
from app.config import Settings


@pytest.fixture
def kokoro_factory(monkeypatch):
    """Replace app.agent.tts.KokoroVietnamese with a controllable stub."""
    calls: list[dict] = []
    instances: dict[str, SimpleNamespace] = {}

    def factory(**kwargs):
        calls.append(kwargs)
        voice = kwargs["voice"]
        inst = SimpleNamespace(
            device=kwargs["device"],
            synthesize=lambda text, *_a, **_k: (np.zeros(24000, dtype=np.float32), "ph"),
        )
        instances[voice] = inst
        return inst

    monkeypatch.setattr(tts, "KokoroVietnamese", factory)
    return {"factory": factory, "calls": calls, "instances": instances}


def test_invalid_device_rejected():
    with pytest.raises(ValueError):
        TTSEngine(device="gpu-typo")
    with pytest.raises(ValueError):
        Settings(_env_file=None, tts_device="auto")


def test_cpu_engine_uses_cpu_and_writes_wav(kokoro_factory, tmp_path):
    wav = tmp_path / "out.wav"
    engine = TTSEngine(device="cpu")
    seconds = __import__("asyncio").run(engine.synthesize("An bước vào rừng.", "diem_trinh", str(wav)))
    assert kokoro_factory["calls"][0]["device"] == "cpu"
    assert seconds == pytest.approx(1.0)
    assert wav.exists() and wav.stat().st_size > 0


def test_voice_guard_raises_for_unknown_voice(kokoro_factory, tmp_path):
    engine = TTSEngine(device="cpu")
    with pytest.raises(TTSError, match="Giọng đọc không hợp lệ"):
        __import__("asyncio").run(engine.synthesize("x", "khong-ton-tai", str(tmp_path / "a.wav")))
    assert kokoro_factory["calls"] == []
    assert list(tmp_path.iterdir()) == []


def test_caches_one_instance_per_voice(kokoro_factory, tmp_path):
    engine = TTSEngine(device="cpu")
    a1 = engine._instance("diem_trinh")
    a2 = engine._instance("diem_trinh")
    assert a1 is a2  # same voice reused
    b = engine._instance("my_yen")
    assert b is not a1
    assert len(kokoro_factory["calls"]) == 2


def test_missing_local_model_dir_is_explicit_error(tmp_path):
    missing = tmp_path / "nope"
    engine = TTSEngine(model_dir=missing, device="cpu")
    with pytest.raises(TTSError, match="Thiếu model Kokoro"):
        engine._instance("diem_trinh")


def test_voices_expose_kokoro_labels():
    assert "diem_trinh" in VOICES and VOICES["diem_trinh"] == "Diễm Trinh"
    assert len(VOICES) == 14
    assert all(isinstance(label, str) and label for label in VOICES.values())


def test_local_model_dir_uses_local_paths(kokoro_factory, tmp_path):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "kokoro_vi.pth").write_bytes(b"x")
    engine = TTSEngine(model_dir=tmp_path, device="cpu")
    engine._instance("diem_trinh")
    kwargs = kokoro_factory["calls"][0]
    assert str(Path(kwargs["config_path"]).resolve()) == str((tmp_path / "config.json").resolve())
    assert str(Path(kwargs["model_path"]).resolve()) == str((tmp_path / "kokoro_vi.pth").resolve())