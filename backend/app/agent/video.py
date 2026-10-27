"""FFmpeg rendering with durable state, input fencing and atomic publication."""
import asyncio
import math
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..db import Session
from ..models import Chapter, Event, Video

BACKGROUND_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


class VideoError(RuntimeError):
    pass


def background_path(name: str) -> Path:
    root = Path(settings.video_background_dir).resolve()
    if not name or Path(name).name != name or Path(name).suffix.lower() not in BACKGROUND_EXTENSIONS:
        raise VideoError("Tên video nền không hợp lệ")
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise VideoError("Không tìm thấy video nền")
    return path


def backgrounds() -> list[str]:
    root = Path(settings.video_background_dir).resolve()
    if not root.is_dir():
        return []
    names = []
    for path in sorted(root.iterdir()):
        try:
            background_path(path.name)
        except VideoError:
            continue
        names.append(path.name)
    return names


def default_background() -> str:
    names = backgrounds()
    if not names:
        raise VideoError("Chưa có video nền; thêm file vào thư mục video-backgrounds")
    return names[0]


def audio_matches(video: Video, chapter: Chapter | None) -> bool:
    return bool(chapter and chapter.audio_status == "ready" and chapter.audio_path
                and chapter.version == chapter.audio_version == video.audio_version
                and chapter.audio_generation == video.audio_generation
                and chapter.audio_voice == video.voice)


async def invalidate_video(session, project_id: str, n: int):
    """Caller locks the chapter first; never acquire chapter after video."""
    video = await session.scalar(select(Video).where(
        Video.project_id == project_id, Video.chapter_number == n).with_for_update())
    if video:
        video.generation += 1
        video.status, video.error = "stale", None
        video.path, video.duration, video.bytes = None, None, None


async def render(background: Path, audio: Path, duration: float, output: Path):
    """Bounded subprocess, killed/reaped on cancellation before temp cleanup."""
    if not math.isfinite(duration) or duration <= 0:
        raise VideoError("Thời lượng audio không hợp lệ")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(background),
        "-i", str(audio), "-filter_complex",
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-t", str(duration), "-shortest",
        "-movflags", "+faststart", str(output),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(6900):
            code = await process.wait()
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if code != 0 or not output.is_file() or output.stat().st_size < 100:
        raise VideoError("FFmpeg không tạo được video; kiểm tra file nền và audio")


async def _locked_inputs(session, project_id: str, n: int):
    chapter = await session.scalar(select(Chapter).where(
        Chapter.project_id == project_id, Chapter.chapter_number == n).with_for_update())
    video = await session.scalar(select(Video).where(
        Video.project_id == project_id, Video.chapter_number == n).with_for_update())
    return chapter, video


async def run_video(project_id: str, n: int, generation: int):
    """Worker owns a PG execution lock; each DB write rechecks generation/input."""
    async with Session() as s, s.begin():
        chapter, video = await _locked_inputs(s, project_id, n)
        if not video or video.generation != generation or video.status not in ("queued", "processing"):
            return
        if not audio_matches(video, chapter):
            video.status = "stale"
            return
        audio_name, duration, background = chapter.audio_path, chapter.audio_seconds, video.background
        version, audio_generation, voice = video.audio_version, video.audio_generation, video.voice
        video.status, video.error = "processing", None
        s.add(Event(project_id=project_id, type="video_processing", payload={"chapter_number": n}))
    try:
        audio = Path(audio_name).resolve()
        if not audio.is_relative_to(Path(settings.audio_dir).resolve()) or not audio.is_file():
            raise VideoError("File audio không còn hoặc đường dẫn không hợp lệ; tổng hợp lại")
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise VideoError("Thời lượng audio không hợp lệ")
        background_file = background_path(background)
        directory = Path(settings.video_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="video-", dir=directory) as tmp:
            output = Path(tmp, "video.mp4")
            await render(background_file, audio, duration, output)
            if not output.is_file() or output.stat().st_size < 100:
                raise VideoError("File video rỗng")
            async with Session() as s, s.begin():
                chapter, video = await _locked_inputs(s, project_id, n)
                if not video or video.generation != generation or video.status != "processing":
                    return
                if (not audio_matches(video, chapter) or video.audio_version != version
                        or video.audio_generation != audio_generation or video.voice != voice):
                    video.status = "stale"
                    return
                filename = f"{video.id}-v{version}-a{audio_generation}-g{generation}.mp4"
                size = output.stat().st_size
                os.replace(output, directory / filename)
                video.status, video.path = "ready", str(directory / filename)
                video.duration, video.width, video.height, video.bytes = round(duration, 2), 1920, 1080, size
                s.add(Event(project_id=project_id, type="video_ready", payload={
                    "chapter_number": n, "video_id": video.id, "seconds": video.duration}))
    except Exception as exc:
        detail = (str(exc) if isinstance(exc, VideoError)
                  else f"Render thất bại ({type(exc).__name__}); kiểm tra FFmpeg và thư mục video.")
        async with Session() as s, s.begin():
            chapter, video = await _locked_inputs(s, project_id, n)
            if video and video.generation == generation and video.status == "processing":
                if not audio_matches(video, chapter):
                    video.status = "stale"
                    return
                video.status, video.error = "failed", detail
                s.add(Event(project_id=project_id, type="video_failed",
                            payload={"chapter_number": n, "error": detail}))
