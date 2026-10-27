"""Audio API/worker regressions; synthetic WAV fixtures, never live Kokoro.

The fake engine writes a deterministic PCM tone. MP3 encoding/decoding uses
real ffmpeg, so these tests verify the pipeline, not TTS voice quality.
"""

import asyncio
import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.agent.tts as tts
from app.agent.workflow import run_story
from app.main import app
from app.models import Chapter, Event, Job, Outline, Project


@pytest.fixture
def synthetic_wav_engine():
    """Explicit test-only synthesizer: 0.4 s of 440 Hz PCM, no model/network."""
    class SyntheticWavEngine:
        def __init__(self):
            self.calls = []
            self.entered = asyncio.Event()
            self.release = None

        async def synthesize(self, text, voice, wav_path):
            self.calls.append((text, voice))
            self.entered.set()
            if self.release is not None:
                await self.release.wait()
            sample_rate, frames = 16000, 6400
            pcm = b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
                for i in range(frames)
            )
            with wave.open(wav_path, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                output.writeframes(pcm)
            return frames / sample_rate

    return SyntheticWavEngine()


@pytest.fixture(autouse=True)
def audio_environment(db, mock_mode, monkeypatch, tmp_path, synthetic_wav_engine):
    # conftest patches the story worker but not the audio worker's Session alias.
    monkeypatch.setattr(tts, "Session", db)
    monkeypatch.setattr(tts, "engine", synthetic_wav_engine)
    monkeypatch.setattr(mock_mode, "audio_dir", str(tmp_path / "audio"))
    monkeypatch.setattr(mock_mode, "tts_voice", "diem_trinh")
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    monkeypatch.setattr(app.state, "redis", redis, raising=False)
    return SimpleNamespace(directory=tmp_path / "audio", redis=redis)


@pytest.fixture
def real_ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("real ffmpeg is required for MP3 integration tests")
    return executable


@pytest_asyncio.fixture
async def chapter(db):
    async with db() as session, session.begin():
        project = Project(title="Audio fixture", prompt="Một truyện kiểm thử audio.", status="completed")
        session.add(project)
        await session.flush()
        session.add(Job(project_id=project.id, status="done"))
        chapter = Chapter(
            project_id=project.id, chapter_number=1, title="Chương kiểm thử",
            content="An bước vào rừng. Gió thổi qua những tán cây xanh.",
            version=1, status="done",
        )
        session.add(chapter)
        await session.flush()
    return chapter


def chapter_url(chapter):
    return f"/api/projects/{chapter.project_id}/chapters/{chapter.chapter_number}"


async def stored_chapter(db, chapter):
    async with db() as session:
        return await session.get(Chapter, chapter.id)


async def audio_events(db):
    async with db() as session:
        return list((await session.scalars(
            select(Event).where(Event.type.like("audio_%")).order_by(Event.id)
        )).all())


async def enqueue_audio(client, chapter, voice="diem_trinh"):
    response = await client.post(chapter_url(chapter) + "/audio", params={"voice": voice})
    assert response.status_code == 202, response.text
    return response


async def produce_audio(client, db, chapter):
    await enqueue_audio(client, chapter)
    current = await stored_chapter(db, chapter)
    await tts.run_audio(chapter.project_id, 1, current.audio_generation)
    return await stored_chapter(db, chapter)


async def test_voices_endpoint_has_explicit_allowlist(client):
    response = await client.get("/api/audio/voices")
    assert response.status_code == 200
    voices = response.json()["voices"]
    expected = {"diem_trinh", "hung_thinh", "mai_linh", "mai_loan", "manh_dung", "my_yen",
                "ngoc_huyen", "phat_tai", "thanh_dat", "thuc_trinh", "tuan_ngoc", "storyvert",
                "duc_an", "duc_duy"}
    assert {voice["id"] for voice in voices} == expected
    assert len(voices) == len(expected)
    assert all(voice["name"].strip() for voice in voices)
    assert response.json()["default_voice"] == "diem_trinh"


@pytest.mark.parametrize("voice", ["diem_trinh", "hung_thinh", "mai_linh", "mai_loan", "manh_dung",
                                   "my_yen", "ngoc_huyen", "phat_tai", "thanh_dat", "thuc_trinh",
                                   "tuan_ngoc", "storyvert", "duc_an", "duc_duy"])
async def test_allowlisted_voice_can_be_queued(client, db, chapter, voice):
    await enqueue_audio(client, chapter, voice)
    assert (await stored_chapter(db, chapter)).audio_voice == voice


@pytest.mark.parametrize("voice", ["unknown", "MAICHI", "../model", "maichi "])
async def test_invalid_voice_rejected_without_queue_mutation(client, db, chapter, audio_environment, voice):
    response = await client.post(chapter_url(chapter) + "/audio", params={"voice": voice})
    assert response.status_code == 422
    current = await stored_chapter(db, chapter)
    assert current.audio_status == "none" and current.audio_generation == 0
    assert await audio_events(db) == []
    audio_environment.redis.enqueue_job.assert_not_awaited()


async def test_enqueue_is_durable_when_redis_fails(client, db, chapter, audio_environment):
    audio_environment.redis.enqueue_job.side_effect = ConnectionError("test Redis unavailable")
    response = await enqueue_audio(client, chapter)
    assert response.json() == {"status": "queued", "chapter_number": 1}
    current = await stored_chapter(db, chapter)
    assert current.audio_status == "queued"
    assert current.audio_generation == 1 and current.audio_version == current.version
    assert current.audio_voice == "diem_trinh"
    assert current.audio_path is None and current.audio_error is None and current.audio_seconds is None
    audio_environment.redis.enqueue_job.assert_awaited_once_with(
        "synthesize_chapter_task", chapter.project_id, 1, 1, _job_id=f"audio:{chapter.id}:1"
    )
    events = await audio_events(db)
    assert [event.type for event in events] == ["audio_queued"]
    assert events[0].payload == {"chapter_number": 1, "voice": "diem_trinh"}
    status = (await client.get(chapter_url(chapter) + "/audio/status")).json()
    assert status["audio_status"] == "queued" and status["version"] == 1


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
async def test_story_must_be_done_before_audio(client, db, chapter, audio_environment, status):
    async with db() as session, session.begin():
        job = await session.scalar(select(Job).where(Job.project_id == chapter.project_id))
        job.status = status
    response = await client.post(chapter_url(chapter) + "/audio")
    assert response.status_code == 409
    current = await stored_chapter(db, chapter)
    assert current.audio_status == "none" and current.audio_generation == 0
    assert await audio_events(db) == []
    audio_environment.redis.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("status", ["queued", "processing"])
async def test_duplicate_enqueue_rejected(client, db, chapter, audio_environment, status):
    await enqueue_audio(client, chapter)
    async with db() as session, session.begin():
        current = await session.get(Chapter, chapter.id)
        current.audio_status = status
    response = await client.post(chapter_url(chapter) + "/audio", params={"voice": "my_yen"})
    assert response.status_code == 409
    current = await stored_chapter(db, chapter)
    assert current.audio_status == status and current.audio_generation == 1
    assert current.audio_voice == "diem_trinh"
    assert [event.type for event in await audio_events(db)] == ["audio_queued"]
    audio_environment.redis.enqueue_job.assert_awaited_once()


async def test_run_audio_publishes_real_decodable_mp3(
    client, db, chapter, audio_environment, synthetic_wav_engine, real_ffmpeg,
):
    current = await produce_audio(client, db, chapter)
    assert current.audio_status == "ready" and current.audio_error is None
    assert current.audio_version == chapter.version and current.audio_seconds == pytest.approx(0.4)
    path = Path(current.audio_path)
    assert path.parent == audio_environment.directory.resolve()
    assert path.name == f"{chapter.id}-v1-g1.mp3" and path.stat().st_size > 100
    decoded = subprocess.run(
        [real_ffmpeg, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True, timeout=15,
    )
    assert len(decoded.stdout) > 0 and any(decoded.stdout)
    assert len(decoded.stdout) / (2 * 16000) == pytest.approx(0.4, abs=0.1)
    assert synthetic_wav_engine.calls == [(chapter.content, "diem_trinh")]
    assert sorted(p.name for p in audio_environment.directory.iterdir()) == [path.name]
    assert [event.type for event in await audio_events(db)] == ["audio_queued", "audio_processing", "audio_ready"]
    # Redelivering a completed generation must not synthesize or emit events again.
    await tts.run_audio(chapter.project_id, 1, 1)
    assert len(synthetic_wav_engine.calls) == 1
    assert len(await audio_events(db)) == 3


async def test_download_supports_valid_byte_range(client, db, chapter, real_ffmpeg):
    current = await produce_audio(client, db, chapter)
    contents = Path(current.audio_path).read_bytes()
    url = chapter_url(chapter) + "/audio"
    full = await client.get(url)
    assert full.status_code == 200 and full.content == contents
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["cache-control"] == "no-cache"
    assert full.headers["accept-ranges"] == "bytes"
    response = await client.get(url, headers={"Range": "bytes=10-99"})
    assert response.status_code == 206
    assert response.content == contents[10:100]
    assert response.headers["content-range"] == f"bytes 10-99/{len(contents)}"
    assert response.headers["content-length"] == "90"


async def test_edit_invalidates_ready_audio(client, db, chapter, real_ffmpeg):
    ready = await produce_audio(client, db, chapter)
    response = await client.put(chapter_url(chapter), json={
        "title": chapter.title, "content": "Nội dung mới hoàn toàn.", "version": chapter.version,
    })
    assert response.status_code == 200, response.text
    current = await stored_chapter(db, chapter)
    assert current.version == ready.version + 1
    assert current.audio_generation == ready.audio_generation + 1
    assert current.audio_status == "stale"
    assert current.audio_path is None and current.audio_seconds is None and current.audio_error is None
    assert (await client.get(chapter_url(chapter) + "/audio")).status_code == 404
    assert (await client.get(chapter_url(chapter) + "/audio/status")).json()["audio_status"] == "stale"


async def test_edit_fences_inflight_output(
    client, db, chapter, audio_environment, synthetic_wav_engine, real_ffmpeg,
):
    await enqueue_audio(client, chapter)
    synthetic_wav_engine.release = asyncio.Event()
    worker = asyncio.create_task(tts.run_audio(chapter.project_id, 1, 1))
    try:
        await asyncio.wait_for(synthetic_wav_engine.entered.wait(), timeout=5)
        assert (await stored_chapter(db, chapter)).audio_status == "processing"
        response = await client.put(chapter_url(chapter), json={
            "title": chapter.title, "content": "Bản sửa trong lúc tổng hợp.", "version": 1,
        })
        assert response.status_code == 200, response.text
        synthetic_wav_engine.release.set()
        await asyncio.wait_for(worker, timeout=15)
    finally:
        synthetic_wav_engine.release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    current = await stored_chapter(db, chapter)
    assert current.version == 2 and current.audio_generation == 2
    assert current.audio_status == "stale" and current.audio_path is None and current.audio_seconds is None
    assert list(audio_environment.directory.iterdir()) == []
    assert [event.type for event in await audio_events(db)] == ["audio_queued", "audio_processing"]
    assert (await client.get(chapter_url(chapter) + "/audio")).status_code == 404


@pytest.mark.parametrize("failure", ["missing_model", "internal_error"])
async def test_audio_failure_is_sanitized(client, db, chapter, monkeypatch, tmp_path, failure, audio_environment):
    if failure == "missing_model":
        # A guaranteed nonexistent directory exercises real model validation,
        # without loading Kokoro or touching the parent's hardware/model files.
        monkeypatch.setattr(tts, "engine", tts.TTSEngine(model_dir=str(tmp_path / "missing-model")))
        expected = "Thiếu model Kokoro; kiểm tra thư mục model."
    else:
        monkeypatch.setattr(tts, "engine", SimpleNamespace(
            synthesize=AsyncMock(side_effect=RuntimeError("internal test path /private/test-model"))
        ))
        expected = "Tổng hợp thất bại (RuntimeError); kiểm tra model và thư mục audio."
    await enqueue_audio(client, chapter)
    await tts.run_audio(chapter.project_id, 1, 1)
    current = await stored_chapter(db, chapter)
    assert current.audio_status == "failed" and current.audio_error == expected
    assert current.audio_path is None and current.audio_seconds is None
    events = await audio_events(db)
    assert [event.type for event in events] == ["audio_queued", "audio_processing", "audio_failed"]
    assert events[-1].payload == {"chapter_number": 1, "error": expected}
    response = await client.get(chapter_url(chapter) + "/audio/status")
    assert response.json()["error"] == expected
    assert str(tmp_path) not in response.text and "/private/" not in response.text
    assert "Traceback" not in response.text
    assert list(audio_environment.directory.iterdir()) == []


@pytest.mark.parametrize("text", [
    "x" * 2003,
    "Một câu ngắn. Câu tiếp theo! Câu hỏi? Và tiếp tục…\n" * 40,
    "Tiếng Việt: trời mưa, đường vắng. 🌧️ 漢字 こんにちは e\u0301\t" * 40,
    "  \n\t ",
    "Một câu ngắn.",
])
@pytest.mark.parametrize("limit", [60, 150, 400])
def test_split_text_preserves_nonwhitespace_and_bounds_chunks(text, limit):
    chunks = tts.split_text(text, limit)
    assert all(chunk.strip() == chunk and 0 < len(chunk) <= limit for chunk in chunks)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", text)
    if len(text.strip()) > limit:
        assert len(chunks) > 1
    if not text.strip():
        assert chunks == []


def test_split_keeps_short_prose_as_one_reading():
    text = "Một câu ngắn. Câu này hơi dài hơn một chút, nhưng vẫn ổn. Câu cuối."
    assert tts.split_text(text, 150) == [text]


def test_split_closes_every_grouping_at_sentence_end():
    # Repeating short sentences: chunks must always break only after " . " never mid-sentence.
    text = "Trời xanh. Gió nhẹ. " * 20
    for limit in (30, 60, 150):
        chunks = tts.split_text(text, limit)
        assert all(c.endswith(".") and c.strip() == c and 0 < len(c) <= limit for c in chunks)
        joined = "".join(chunks)
        assert joined.count("Trời xanh.") == 20 and joined.count("Gió nhẹ.") == 20
        assert re.sub(r"\s", "", joined) == re.sub(r"\s", "", text)


def test_split_handles_questions_exclaims_ellipsis_without_mid_cut():
    text = "Có thật không? Không! Rồi cô ấy… lặng im. Hết."
    assert tts.split_text(text, 200) == [text]
    # Ellipsis mid-thought is a pause, not a cut: "Rồi cô ấy… lặng im." stays whole.
    assert tts.split_text(text, 20) == ["Có thật không?", "Không!", "Rồi cô ấy… lặng im.", "Hết."]
    assert tts.split_text(text, 60) == [text]
    # A genuine trailing ellipsis it IS a boundary when the next word is capitalized.
    block = ("Cô ấy chùng lại…\n" + "Anh bước tiếp. ") * 10
    chunks = tts.split_text(block, 60)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", block)


def test_split_keeps_closing_quote_with_its_sentence():
    text = 'Anh thì thầm "Đi thôi." Rồi bước tiếp.'
    assert tts.split_text(text, 200) == [text]
    # A forced split must keep the whole quoted sentence in exactly one chunk.
    long_text = text + " " + "x" * 120
    chunks = tts.split_text(long_text, 100)
    assert sum(1 for c in chunks if text in c) == 1
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", long_text)


def test_split_overlong_sentence_breaks_at_clause_not_mid_word():
    text = "Một đoạn rất dài" + ", còn nữa, vẫn chưa kết thúc, kéo dài ra ngoài giới hạn" * 5 + "."
    chunks = tts.split_text(text, 80)
    assert all(0 < len(c) <= 80 and c.strip() == c for c in chunks)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", text)
    # Every piece except the final sentence-tail ends at a clause separator.
    for chunk in chunks[:-1]:
        assert chunk.endswith((",", ";", ":")), chunk
    assert chunks[-1].endswith(".")


def test_split_falls_back_to_word_boundary_never_splits_ascii_word_lines():
    # No clause separators and no sentence enders => still word-aligned.
    text = " ".join(["word"] * 500)
    for limit in (60, 150):
        chunks = tts.split_text(text, limit)
        assert all(0 < len(c) <= limit for c in chunks)
        assert all(not c.startswith((" ", ",")) for c in chunks)
        assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", text)


@pytest.mark.parametrize(("mode", "expected_count"), [("short", 1), ("long", 3)])
async def test_story_mode_short_forces_one_long_keeps_multiple(db, mock_mode, mode, expected_count):
    async with db() as session, session.begin():
        project = Project(
            title="Mode regression", prompt="Một truyện về An đi tìm bạn, gồm ba chương.",
            options={"mode": mode, "chapter_count": 3, "target_words": 800}, status="queued",
        )
        session.add(project)
        await session.flush()
        session.add(Job(project_id=project.id, status="queued", generation=1))
        project_id = project.id
    await run_story(project_id, 1)
    async with db() as session:
        project = await session.get(Project, project_id)
        job = await session.scalar(select(Job).where(Job.project_id == project_id))
        chapters = (await session.scalars(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
        )).all()
        outlines = (await session.scalars(select(Outline).where(Outline.project_id == project_id))).all()
    assert job.status == "done"
    assert project.spec["mode"] == mode and project.spec["chapter_count"] == expected_count
    assert [chapter.chapter_number for chapter in chapters] == list(range(1, expected_count + 1))
    assert len(outlines) == expected_count
