"""Video render + publish orchestration via the real FastAPI app (sqlite, patched redis).

Render paths that need real ffmpeg are exercised with a generated testsrc background;
publish orchestration is driven by the durable DB state plus a mocked provider client.
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.agent.publish as publish_mod
import app.agent.video as video_mod
import app.main as main_module
from app.agent.video import run_video
from app.models import Chapter, Job, Publish, SocialAccount, Video


async def make_audio_ready_chapter(db):
    async with db() as session, session.begin():
        from app.models import Project
        project = Project(title="Video fixture", prompt="Một truyện kiểm thử video.",
                          status="completed")
        session.add(project)
        await session.flush()
        session.add(Job(project_id=project.id, status="done"))
        chapter = Chapter(
            project_id=project.id, chapter_number=1, title="Chương video", content="Nội dung chương.",
            version=1, status="done", audio_status="ready",
            audio_generation=1, audio_version=1, audio_voice="maichi",
            audio_path="/tmp/no-such-audio.mp3", audio_seconds=3.0,
        )
        session.add(chapter)
        await session.flush()
        return chapter


@pytest_asyncio.fixture
async def chapter(db, tmp_path):
    chapter = await make_audio_ready_chapter(db)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"\xff\xfb\x90\x64" * 100)  # dummy mp3 header bytes
    async with db() as session, session.begin():
        c = await session.get(Chapter, chapter.id)
        c.audio_path = str(audio)
    return chapter


@pytest.fixture(autouse=True)
def video_environment(db, monkeypatch, tmp_path):
    # patch video module Session + dirs; swap run_video's real call for a local dir.
    audio_dir = tmp_path / "audio"
    music_dir = tmp_path / "videos"
    bg_dir = tmp_path / "video-backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(video_mod, "Session", db)
    monkeypatch.setattr(video_mod.settings, "video_dir", str(music_dir))
    monkeypatch.setattr(video_mod.settings, "video_background_dir", str(bg_dir))
    monkeypatch.setattr(video_mod.settings, "audio_dir", str(audio_dir))
    monkeypatch.setattr(publish_mod, "Session", db)
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    monkeypatch.setattr(main_module.app.state, "redis", redis, raising=False)
    return SimpleNamespace(backgrounds=bg_dir, videos=music_dir, redis=redis)


def _write_background(video_environment, name="bg.mov"):
    bg = Path(video_environment.backgrounds) / name
    bg.write_bytes(b"\x00" * 50)
    return bg


@pytest.fixture
def real_ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("real ffmpeg required")
    return executable


# ---- video API ----

async def test_create_video_requires_done_job(client, db, chapter, video_environment):
    async with db() as session, session.begin():
        job = await session.scalar(select(Job).where(Job.project_id == chapter.project_id))
        job.status = "running"
    resp = await client.post(f"/api/projects/{chapter.project_id}/chapters/1/video")
    assert resp.status_code == 409


async def test_create_video_enqueues_render(client, db, chapter, video_environment):
    _write_background(video_environment)
    resp = await client.post(f"/api/projects/{chapter.project_id}/chapters/1/video",
                             json={"background": "bg.mov"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    video_environment.redis.enqueue_job.assert_awaited_once()
    name = video_environment.redis.enqueue_job.await_args.args[0]
    assert name == "render_video_task"


async def test_video_status_none_without_create(client, db, chapter, video_environment):
    resp = await client.get(f"/api/projects/{chapter.project_id}/chapters/1/video/status")
    assert resp.status_code == 200
    assert resp.json()["video_status"] == "none"


async def test_video_render_e2e_real_ffmpeg(client, db, chapter, video_environment, real_ffmpeg):
    """Render via run_video with a mocked render that emits a real MP4, then ffprobe it."""
    bg = Path(video_environment.backgrounds) / "bg.mov"
    subprocess.run([real_ffmpeg, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
                    "-c:v", "libx264", str(bg)], check=True, timeout=40)
    # A real audio file must exist inside the (patched) audio dir for run_video's
    # path validation, even though render is mocked.
    audio_dir = Path(video_environment.backgrounds).parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_file = audio_dir / "ch1.mp3"
    audio_file.write_bytes(b"\xff\xfb\x90\x64" * 100)
    async with db() as session, session.begin():
        c = await session.get(Chapter, chapter.id)
        c.audio_path = str(audio_file)

    resp = await client.post(f"/api/projects/{chapter.project_id}/chapters/1/video",
                             json={"background": "bg.mov"})
    assert resp.status_code == 202, resp.text
    video_id = resp.json()["video_id"]
    with patch.object(video_mod, "render", new=fake_render_mp4):
        await run_video(chapter.project_id, 1, 1)
    async with db() as session:
        v = await session.get(Video, video_id)
        assert v.status == "ready" and v.path and v.path.startswith(
            str(Path(video_environment.videos).resolve()))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", v.path], capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    import json
    info = json.loads(probe.stdout)
    assert any(s["codec_type"] == "video" for s in info["streams"])


async def test_get_video_404_without_render(client, db, chapter, video_environment):
    _write_background(video_environment)
    await client.post(f"/api/projects/{chapter.project_id}/chapters/1/video",
                      json={"background": "bg.mov"})
    resp = await client.get(f"/api/projects/{chapter.project_id}/chapters/1/video")
    assert resp.status_code == 404


# ---- publish orchestration ----

@pytest_asyncio.fixture
async def ready_video(db, chapter, video_environment):
    _write_background(video_environment)
    async with db() as session, session.begin():
        video = Video(project_id=chapter.project_id, chapter_number=1,
                      audio_version=1, audio_generation=1, voice="maichi",
                      background="bg.mov", status="ready", path=str(video_environment.videos / "vid.mp4"),
                      duration=3.0, width=1920, height=1080, bytes=100, generation=1)
        session.add(video)
        await session.flush()
        return video


@pytest_asyncio.fixture
async def connected_tiktok(db):
    async with db() as session, session.begin():
        account = SocialAccount(platform="tiktok", provider_account_id="acc-1",
                                display_name="Vũ", access_token="tok", scopes=["video.publish"])
        session.add(account)
        await session.flush()


async def test_publish_requires_connected_account(client, db, ready_video, video_environment):
    resp = await client.post(f"/api/videos/{ready_video.id}/publish",
                             json={"platform": "tiktok", "caption": "hi"})
    assert resp.status_code == 409


async def test_publish_pipeline_queues_then_finalizes(client, db, ready_video, video_environment,
                                                      connected_tiktok):
    resp = await client.post(f"/api/videos/{ready_video.id}/publish",
                             json={"platform": "tiktok", "caption": "chào", "privacy": "SELF_ONLY"})
    assert resp.status_code == 202, resp.text
    pid = resp.json()["publish_id"]
    async with db() as session:
        publish = await session.get(Publish, pid)
        assert publish.status == "queued"
    # Fake the provider upload path inside publish_mod.
    fake_status = {"remote_id": "remote-1", "processing": True}
    with patch.object(publish_mod, "_publish_tiktok", new=AsyncMock(return_value=fake_status)):
        await publish_mod.run_publish(pid, publish.generation)
    async with db() as session:
        publish = await session.get(Publish, pid)
        assert publish.status == "processing"
        assert publish.remote_id == "remote-1"


async def test_publish_disconnect(client, db, ready_video, video_environment, connected_tiktok):
    resp = await client.delete("/api/social/tiktok")
    assert resp.status_code == 200
    async with db() as session:
        assert await session.scalar(select(SocialAccount).where(
            SocialAccount.platform == "tiktok")) is None


async def test_social_status_reflects_connections(client, db, connected_tiktok):
    resp = await client.get("/api/social/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tiktok"]["connected"] is True
    assert body["tiktok"]["display_name"] == "Vũ"
    assert body["youtube"]["connected"] is False


async def _real_render(*args, **kwargs):
    """Fallback not used; kept for clarity."""
    raise AssertionError("render should be mocked")


async def fake_render_mp4(background, audio, duration, output):
    """Produce a real MP4 via ffmpeg on testsrc; audio is ignored (render is mocked)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={max(duration, 0.5)}:size=1920x1080:rate=30",
         "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)],
        check=True, timeout=60)
    if output.stat().st_size < 100:
        raise RuntimeError("empty output")