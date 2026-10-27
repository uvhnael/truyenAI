import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from arq import create_pool
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from .agent.video import VideoError as VideoRenderError
from .agent.video import backgrounds, default_background, invalidate_video
from .agent.workflow import asdict, chapter_row, event, save_revision
from .config import settings
from .db import Session
from .migrate import migrate
from .models import Chapter, Character, Event, Job, Outline, Project, Publish, Revision, SocialAccount, Video
from .queue import enqueue, redis_settings


@asynccontextmanager
async def lifespan(app):
    await migrate()
    app.state.redis = await create_pool(redis_settings)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="truyenAI", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)


class Options(BaseModel):
    mode: Literal["short", "long"] | None = None
    chapter_count: int | None = Field(default=None, ge=1, le=30)
    target_words: int | None = Field(default=None, ge=200, le=5000)


class CreateProject(BaseModel):
    title: str = Field(default="Truyện chưa đặt tên", min_length=1, max_length=200)
    prompt: str = Field(min_length=10, max_length=20000)
    options: Options = Field(default_factory=Options)


class EditChapter(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    title: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=1)


class VideoRequest(BaseModel):
    background: str | None = None


class PublishRequest(BaseModel):
    platform: Literal["tiktok", "youtube"]
    caption: str = Field(default="", max_length=2200)
    hashtags: list[str] = Field(default_factory=list)
    privacy: str | None = None
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    category_id: str = "22"
    schedule_at: datetime | None = None


async def must_project(s, project_id, lock=False):
    stmt = select(Project).where(Project.id == project_id)
    p = await s.scalar(stmt.with_for_update() if lock else stmt)
    if not p:
        raise HTTPException(404, "Không tìm thấy truyện")
    return p


async def dispatch(job):
    try:
        await enqueue(app.state.redis, job)
    except Exception:
        # Do not lose committed work: worker's periodic DB sweep re-enqueues it.
        pass


@app.get("/api/health")
async def health():
    async with Session() as s:
        await s.execute(text("SELECT 1"))
    try:
        await app.state.redis.ping()
        redis_ok = True
        worker_ok = bool(await app.state.redis.exists("arq:queue:health-check"))
    except Exception:
        redis_ok, worker_ok = False, False
    return {
        "status": "ok" if redis_ok and worker_ok else "degraded",
        "database": "ok",
        "redis": redis_ok,
        "worker": worker_ok,
        "llm_mode": "mock" if settings.mock else "live",
        "llm_model": settings.llm_model,
    }


@app.post("/api/projects", status_code=201)
async def create_project(data: CreateProject):
    if not data.prompt.strip() or not data.title.strip():
        raise HTTPException(422, "Tiêu đề và prompt không được để trống")
    async with Session() as s, s.begin():
        p = Project(
            title=data.title.strip(),
            prompt=data.prompt.strip(),
            options=data.options.model_dump(exclude_none=True),
            status="queued",
        )
        s.add(p)
        await s.flush()
        job = Job(project_id=p.id, status="queued", generation=1)
        s.add(job)
        await s.flush()
        event(s, p.id, job.id, "queued", mode="mock" if settings.mock else "live")
        result = asdict(p)
    await dispatch(job)
    return result


@app.get("/api/projects")
async def list_projects():
    async with Session() as s:
        return [
            asdict(p)
            for p in (await s.scalars(select(Project).order_by(Project.created_at.desc()).limit(500))).all()
        ]


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    async with Session() as s:
        p = await must_project(s, project_id)
        result = asdict(p)
        for name, model in [("characters", Character), ("outline", Outline), ("chapters", Chapter)]:
            stmt = select(model).where(model.project_id == project_id)
            if model in (Outline, Chapter):
                stmt = stmt.order_by(model.chapter_number)
            result[name] = [asdict(row) for row in (await s.scalars(stmt)).all()]
        job = await s.scalar(select(Job).where(Job.project_id == project_id))
        result["job"] = asdict(job) if job else None
        return result


@app.post("/api/projects/{project_id}/cancel")
async def cancel_project(project_id: str):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
        if job and job.status in ("queued", "running"):
            p.status = job.status = "cancelled"
            job.generation += 1  # fence any in-flight LLM result immediately
            event(s, p.id, job.id, "job_cancelled")
        await s.flush()
        await s.refresh(p)
        return asdict(p)


@app.post("/api/projects/{project_id}/run")
async def run_project(project_id: str):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
        if job.status in ("queued", "running"):
            raise HTTPException(409, "Truyện đang được xử lý")
        if job.status == "done":
            raise HTTPException(409, "Truyện đã hoàn tất; chỉnh sửa thủ công hoặc tạo truyện mới")
        job.status = p.status = "queued"
        job.generation += 1
        job.error = None
        event(s, p.id, job.id, "queued", resume=True)
        await s.flush()
        await s.refresh(job)
        result = asdict(job)
    await dispatch(job)
    return result


@app.get("/api/projects/{project_id}/chapters")
async def list_chapters(project_id: str):
    async with Session() as s:
        await must_project(s, project_id)
        return [
            asdict(c)
            for c in (
                await s.scalars(
                    select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
                )
            ).all()
        ]


async def must_chapter(s, project_id, n, lock=False):
    if lock:
        c = await s.scalar(select(Chapter).where(
            Chapter.project_id == project_id, Chapter.chapter_number == n).with_for_update())
    else:
        c = await chapter_row(s, project_id, n)
    if not c:
        raise HTTPException(404, "Không tìm thấy chương")
    return c


@app.get("/api/projects/{project_id}/chapters/{n}")
async def get_chapter(project_id: str, n: int):
    async with Session() as s:
        return asdict(await must_chapter(s, project_id, n))


@app.put("/api/projects/{project_id}/chapters/{n}")
async def edit_chapter(project_id: str, n: int, data: EditChapter):
    if not data.content.strip() or not data.title.strip():
        raise HTTPException(422, "Nội dung và tiêu đề không được để trống")
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
        # Editing only after all agent work ends avoids invalidating downstream memory/checkpoints.
        if job and job.status != "done":
            raise HTTPException(
                409, "Chỉ sửa chương sau khi tác vụ hoàn tất; truyện lỗi/hủy cần tiếp tục trước"
            )
        c = await must_chapter(s, project_id, n)
        if c.version != data.version:
            raise HTTPException(409, "Chương đã thay đổi; tải lại trước khi lưu")
        c.title = data.title.strip()
        await save_revision(
            s, c, data.content, "manual", {"notes": "Người dùng chỉnh sửa; cần kiểm tra lại continuity"}
        )
        c.status = "edited"
        await invalidate_video(s, project_id, n)
        c.audio_generation += 1
        c.audio_status = "stale" if c.audio_status != "none" else "none"
        c.audio_path, c.audio_seconds, c.audio_error = None, None, None
        # A manual edit invalidates the previous automated quality assurance.
        c.summary = ""
        c.memory = {}
        p.status = "needs_review"
        p.final_review = {
            "verdict": "revise",
            "issues": [],
            "notes": "Nội dung đã chỉnh sửa thủ công; đánh giá AI trước không còn hiệu lực.",
        }
        event(s, p.id, job.id if job else None, "chapter_edited", chapter_number=n, version=c.version)
        await s.flush()
        await s.refresh(c)
        return asdict(c)


@app.get("/api/projects/{project_id}/chapters/{n}/revisions")
async def revisions(project_id: str, n: int):
    async with Session() as s:
        c = await must_chapter(s, project_id, n)
        return [
            asdict(r)
            for r in (
                await s.scalars(
                    select(Revision).where(Revision.chapter_id == c.id).order_by(Revision.revision_no.desc())
                )
            ).all()
        ]


@app.get("/api/audio/voices")
async def audio_voices():
    from .agent.tts import VOICES
    return {"voices": [{"id": k, "name": v} for k, v in VOICES.items()], "default_voice": settings.tts_voice}


@app.post("/api/projects/{project_id}/chapters/{n}/audio", status_code=202)
async def create_audio(project_id: str, n: int, voice: str | None = None):
    from .agent.tts import VOICES
    voice = voice or settings.tts_voice
    if voice not in VOICES:
        raise HTTPException(422, "Giọng đọc không hợp lệ")
    async with Session() as s, s.begin():
        await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id))
        if not job or job.status != "done":
            raise HTTPException(409, "Đợi tác vụ viết hoàn tất trước khi chuyển audio")
        c = await must_chapter(s, project_id, n)
        if not (c.content or "").strip():
            raise HTTPException(409, "Chương chưa có nội dung")
        if c.audio_status in ("queued", "processing"):
            raise HTTPException(409, "Audio đang được tổng hợp")
        await invalidate_video(s, project_id, n)
        c.audio_generation += 1
        c.audio_version, c.audio_voice = c.version, voice
        c.audio_status, c.audio_error = "queued", None
        c.audio_path, c.audio_seconds = None, None
        generation, cid = c.audio_generation, c.id
        event(s, project_id, job.id, "audio_queued", chapter_number=n, voice=voice)
    try:
        await app.state.redis.enqueue_job("synthesize_chapter_task", project_id, n, generation,
                                          _job_id=f"audio:{cid}:{generation}")
    except Exception:
        pass  # durable DB queue recovered by worker, even if Redis was unavailable
    return {"status": "queued", "chapter_number": n}


@app.get("/api/projects/{project_id}/chapters/{n}/audio")
async def get_audio(project_id: str, n: int):
    from pathlib import Path
    async with Session() as s:
        c = await must_chapter(s, project_id, n)
        if c.audio_status != "ready" or not c.audio_path or c.audio_version != c.version:
            raise HTTPException(404, "Chưa có audio cho phiên bản chương này")
        path = Path(c.audio_path).resolve()
        if not path.is_relative_to(Path(settings.audio_dir).resolve()):
            raise HTTPException(404, "Đường dẫn audio không hợp lệ")
    if not path.is_file():
        raise HTTPException(410, "File audio không còn; tổng hợp lại")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name,
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/projects/{project_id}/chapters/{n}/audio/status")
async def audio_status(project_id: str, n: int):
    async with Session() as s:
        c = await must_chapter(s, project_id, n)
        return {"chapter_number": n, "audio_status": c.audio_status, "voice": c.audio_voice,
                "seconds": c.audio_seconds, "error": c.audio_error, "version": c.audio_generation}


@app.get("/api/projects/{project_id}/events")
async def events(
    project_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
):
    async with Session() as s:
        await must_project(s, project_id)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError:
        raise HTTPException(400, "Last-Event-ID không hợp lệ")

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            # DB replay + tail avoids pub/sub subscribe races and survives Redis outages.
            async with Session() as s:
                rows = (
                    await s.scalars(
                        select(Event)
                        .where(Event.project_id == project_id, Event.id > cursor)
                        .order_by(Event.id)
                        .limit(200)
                    )
                ).all()
            for row in rows:
                cursor = row.id
                yield f"id: {row.id}\ndata: {json.dumps(jsonable_encoder(asdict(row)), ensure_ascii=False)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects/{project_id}/export", response_class=PlainTextResponse)
async def export(project_id: str):
    async with Session() as s:
        p = await must_project(s, project_id)
        chapters = (
            await s.scalars(
                select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
            )
        ).all()
        content = f"# {p.title}\n\n" + "\n\n".join(
            f"## Chương {c.chapter_number}: {c.title}\n\n{c.content}" for c in chapters
        )
        return PlainTextResponse(
            content, headers={"Content-Disposition": f'attachment; filename="story-{p.id}.md"'}
        )


# ---- Video ----

@app.get("/api/video/backgrounds")
async def video_backgrounds():
    try:
        return {"backgrounds": backgrounds(), "default": backgrounds()[0] if backgrounds() else None}
    except VideoRenderError:
        return {"backgrounds": [], "default": None}


@app.post("/api/projects/{project_id}/chapters/{n}/video", status_code=202)
async def create_video(project_id: str, n: int, data: VideoRequest | None = None):
    data = data or VideoRequest()
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id))
        if not job or job.status != "done":
            raise HTTPException(409, "Đợi tác vụ viết hoàn tất trước khi tạo video")
        c = await must_chapter(s, project_id, n)
        if c.audio_status != "ready" or not c.audio_path or c.audio_version != c.version:
            raise HTTPException(409, "Chưa có audio sẵn sàng cho chương này")
        try:
            bg = data.background or default_background()
        except VideoRenderError as exc:
            raise HTTPException(422, str(exc))
        video = await s.scalar(select(Video).where(
            Video.project_id == project_id, Video.chapter_number == n).with_for_update())
        if video and video.status in ("queued", "processing"):
            raise HTTPException(409, "Video đang được tạo")
        if video and video.status == "ready" and video.audio_version == c.version \
                and video.audio_generation == c.audio_generation and not data.background:
            return {"status": "ready", "video_id": video.id}
        if video:
            video.generation += 1
            video.status, video.error = "queued", None
            video.background, video.path = bg, None
            video.duration, video.bytes = None, None
            video.audio_version, video.audio_generation = c.audio_version, c.audio_generation
            video.voice = c.audio_voice
        else:
            video = Video(project_id=project_id, chapter_number=n, audio_version=c.audio_version,
                          audio_generation=c.audio_generation, voice=c.audio_voice,
                          background=bg, status="queued")
            s.add(video)
        await s.flush()
        generation, vid = video.generation, video.id
        p.status = "ready" if p.status == "done" else p.status
        # p.status not changed; job is done. Only record event.
        event(s, project_id, job.id, "video_queued", chapter_number=n, background=bg)
    try:
        await app.state.redis.enqueue_job("render_video_task", project_id, n, generation,
                                          _job_id=f"video:{vid}:{generation}")
    except Exception:
        pass  # durable DB queue recovered by worker
    return {"status": "queued", "video_id": vid}


@app.get("/api/projects/{project_id}/chapters/{n}/video/status")
async def video_status(project_id: str, n: int):
    async with Session() as s:
        await must_chapter(s, project_id, n)
        video = await s.scalar(select(Video).where(
            Video.project_id == project_id, Video.chapter_number == n))
        if not video:
            return {"video_status": "none"}
        publishes = (await s.scalars(select(Publish).where(
            Publish.video_id == video.id))).all()
        return {"video_status": video.status, "background": video.background,
                "duration": video.duration, "width": video.width, "height": video.height,
                "bytes": video.bytes, "error": video.error, "generation": video.generation,
                "publishes": [asdict(x) for x in publishes]}


@app.get("/api/projects/{project_id}/chapters/{n}/video")
async def get_video(project_id: str, n: int):
    from pathlib import Path
    async with Session() as s:
        await must_chapter(s, project_id, n)
        video = await s.scalar(select(Video).where(
            Video.project_id == project_id, Video.chapter_number == n))
        if not video or video.status != "ready" or not video.path:
            raise HTTPException(404, "Chưa có video cho chương này")
        path = Path(video.path).resolve()
        if not path.is_relative_to(Path(settings.video_dir).resolve()):
            raise HTTPException(404, "Đường dẫn video không hợp lệ")
    if not path.is_file():
        raise HTTPException(410, "File video không còn; tạo lại")
    return FileResponse(path, media_type="video/mp4", filename=path.name,
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/videos")
async def list_videos(project_id: str | None = None):
    async with Session() as s:
        stmt = select(Video)
        if project_id:
            stmt = stmt.where(Video.project_id == project_id)
        videos = (await s.scalars(stmt.order_by(Video.created_at.desc()).limit(500))).all()
        video_ids = [v.id for v in videos]
        publish_rows = []
        if video_ids:
            publish_rows = (await s.scalars(select(Publish).where(
                Publish.video_id.in_(video_ids)))).all()
        publishes: dict[str, list] = {}
        for row in publish_rows:
            publishes.setdefault(row.video_id, []).append(asdict(row))
        return [{"video": asdict(v), "publishes": publishes.get(v.id, [])} for v in videos]


# ---- Social publishing ----

PLATFORMS = ("tiktok", "youtube")


async def require_oauth_config(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404, "Nền tảng không được hỗ trợ")
    key = getattr(settings, "tiktok_client_key" if platform == "tiktok" else "youtube_client_id")
    secret = getattr(settings, "tiktok_client_secret" if platform == "tiktok" else "youtube_client_secret")
    if not key or not secret:
        raise HTTPException(409, f"Chưa cấu hình ứng dụng OAuth cho {platform}")
    return key, secret


def oauth_redirect_uri(platform: str) -> str:
    return f"{settings.social_redirect_base}/api/social/{platform}/callback"


@app.get("/api/social/status")
async def social_status():
    async with Session() as s:
        accounts = (await s.scalars(select(SocialAccount))).all()
        by_platform = {a.platform: a for a in accounts}
        result = {}
        for platform in PLATFORMS:
            account = by_platform.get(platform)
            result[platform] = {"connected": bool(account),
                                "display_name": account.display_name if account else None}
        return result


@app.get("/api/social/{platform}/authorize")
async def social_authorize(platform: str):
    key, _ = await require_oauth_config(platform)
    state = secrets.token_urlsafe(16)
    redirect_uri = oauth_redirect_uri(platform)
    try:
        await app.state.redis.set(f"oauth:state:{state}", platform, ex=600)
    except Exception:
        raise HTTPException(503, "Không lưu được trạng thái đăng nối")
    if platform == "tiktok":
        from .publish.tiktok import tiktok_authorize_url
        return {"url": tiktok_authorize_url(key, redirect_uri, state)}
    from .publish.youtube import google_authorize_url
    return {"url": google_authorize_url(key, redirect_uri, state)}


@app.get("/api/social/{platform}/callback")
async def social_callback(platform: str, code: str, state: str, request: Request):
    if platform not in PLATFORMS:
        return RedirectResponse(settings.web_origin, status_code=302)
    try:
        stored = await app.state.redis.get(f"oauth:state:{state}")
    except Exception:
        stored = None
    if stored != platform:
        return RedirectResponse(f"{settings.web_origin}/publish?error=invalid_state", status_code=302)
    try:
        await app.state.redis.delete(f"oauth:state:{state}")
    except Exception:
        pass
    try:
        key, secret = await require_oauth_config(platform)
        redirect_uri = oauth_redirect_uri(platform)
        if platform == "tiktok":
            from .publish.tiktok import TikTokClient, exchange_tiktok_code
            tokens = await exchange_tiktok_code(code, key, secret, redirect_uri)
            scopes = (tokens.get("scope") or "").split(",")
            async with TikTokClient(tokens["access_token"]) as client:
                info = await client.user_info()
            refresh_token = tokens.get("refresh_token")
            expires_at = None
        else:
            from .publish.youtube import YoutubeClient, exchange_google_code
            tokens = await exchange_google_code(code, key, secret, redirect_uri)
            scopes = (tokens.get("scope") or " ").split()
            async with YoutubeClient(tokens["access_token"]) as client:
                info = await client.channel_info()
            refresh_token = tokens.get("refresh_token")
            expires_at = datetime.now(UTC)
            if tokens.get("expires_in"):
                from datetime import timedelta
                expires_at += timedelta(seconds=int(tokens["expires_in"]))
        account_id = info.get("id") or info.get("open_id")
        display_name = info.get("title") or info.get("display_name") or platform
    except Exception:
        return RedirectResponse(f"{settings.web_origin}/publish?error=connect", status_code=302)
    async with Session() as s, s.begin():
        account = await s.scalar(select(SocialAccount).where(
            SocialAccount.platform == platform).with_for_update())
        if account:
            account.provider_account_id = account_id or account.provider_account_id
            account.display_name = display_name
            account.access_token = tokens["access_token"]
            account.refresh_token = refresh_token or account.refresh_token
            account.expires_at = expires_at
            account.scopes = scopes
        else:
            s.add(SocialAccount(platform=platform, provider_account_id=account_id or "",
                                display_name=display_name, access_token=tokens["access_token"],
                                refresh_token=refresh_token, expires_at=expires_at, scopes=scopes))
    return RedirectResponse(f"{settings.web_origin}/publish?connected={platform}", status_code=302)


@app.delete("/api/social/{platform}")
async def social_disconnect(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404, "Nền tảng không được hỗ trợ")
    from sqlalchemy import delete
    async with Session() as s, s.begin():
        await s.execute(delete(SocialAccount).where(SocialAccount.platform == platform))
    return {"disconnected": platform}


@app.post("/api/videos/{video_id}/publish", status_code=202)
async def publish_video(video_id: str, data: PublishRequest):
    async with Session() as s, s.begin():
        video = await s.scalar(select(Video).where(Video.id == video_id).with_for_update())
        if not video or video.status != "ready" or not video.path:
            raise HTTPException(409, "Video chưa sẵn sàng để đăng")
        account = await s.scalar(select(SocialAccount).where(
            SocialAccount.platform == data.platform))
        if not account:
            raise HTTPException(409, f"Chưa kết nối {data.platform}; hãy đăng nối trước")
        existing = await s.scalar(select(Publish).where(
            Publish.video_id == video.id, Publish.platform == data.platform))
        if existing and existing.status in ("queued", "uploading", "processing"):
            raise HTTPException(409, "Bài đăng đang xử lý")
        if existing:
            existing.generation += 1
            existing.status, existing.error = "queued", None
            existing.caption = data.caption
            existing.title = data.title or video.project_id
            existing.description = data.description
            existing.hashtags = [h.strip() for h in data.hashtags if h.strip()]
            existing.tags = [t.strip() for t in data.tags if t.strip()]
            existing.privacy = data.privacy or "private"
            existing.category_id = data.category_id
            existing.schedule_at = data.schedule_at
            existing.remote_id = existing.remote_url = None
            publish = existing
        else:
            privacy = data.privacy or ("SELF_ONLY" if data.platform == "tiktok" else "private")
            publish = Publish(video_id=video.id, platform=data.platform, caption=data.caption,
                              title=data.title or video.project_id, description=data.description,
                              hashtags=[h.strip() for h in data.hashtags if h.strip()],
                              tags=[t.strip() for t in data.tags if t.strip()],
                              privacy=privacy, category_id=data.category_id,
                              schedule_at=data.schedule_at, video_generation=video.generation,
                              status="queued")
            s.add(publish)
        await s.flush()
        generation, pid = publish.generation, publish.id
        event(s, video.project_id, None, "publish_queued", platform=data.platform, video_id=video.id)
    try:
        await app.state.redis.enqueue_job("publish_task", pid, generation,
                                          _job_id=f"publish:{pid}:{generation}")
    except Exception:
        pass
    return {"status": "queued", "publish_id": pid}


@app.get("/api/publishes/{publish_id}")
async def publish_detail(publish_id: str):
    async with Session() as s:
        publish = await s.scalar(select(Publish).where(Publish.id == publish_id))
        if not publish:
            raise HTTPException(404, "Không tìm thấy bài đăng")
        return asdict(publish)


@app.post("/api/publishes/{publish_id}/retry", status_code=202)
async def publish_retry(publish_id: str):
    async with Session() as s, s.begin():
        publish = await s.scalar(select(Publish).where(Publish.id == publish_id).with_for_update())
        if not publish:
            raise HTTPException(404, "Không tìm thấy bài đăng")
        if publish.status in ("queued", "uploading", "processing"):
            raise HTTPException(409, "Bài đăng đang xử lý")
        account = await s.scalar(select(SocialAccount).where(
            SocialAccount.platform == publish.platform))
        if not account:
            raise HTTPException(409, "Chưa kết nối tài khoản")
        publish.generation += 1
        publish.status, publish.error = "queued", None
        publish.remote_id = publish.remote_url = None
        await s.flush()
        generation, pid = publish.generation, publish.id
        video = await s.get(Video, publish.video_id)
        if video:
            event(s, video.project_id, None, "publish_retry", platform=publish.platform)
    try:
        await app.state.redis.enqueue_job("publish_task", pid, generation,
                                          _job_id=f"publish:{pid}:{generation}")
    except Exception:
        pass
    return {"status": "queued", "publish_id": pid}


# ---- Interactive Writing Flow ----

class SpecUpdate(BaseModel):
    chapter_count: int | None = Field(default=None, ge=1, le=30)
    target_words: int | None = Field(default=None, ge=200, le=5000)
    mode: Literal["short", "long"] | None = None


class BibleUpdate(BaseModel):
    characters: list[dict] = Field(min_length=1, max_length=30)


class OutlineUpdate(BaseModel):
    chapters: list[dict] = Field(min_length=1, max_length=30)


@app.get("/api/projects/{project_id}/pause-state")
async def get_pause_state(project_id: str):
    async with Session() as s:
        p = await must_project(s, project_id)
        return {
            "status": p.status,
            "pause_stage": p.pause_stage,
            "next_chapter": p.next_chapter,
            "spec": p.spec,
            "bible": p.bible,
            "outline": [
                {"chapter_number": o.chapter_number, "title": o.title, "summary": o.summary, "beats": o.beats}
                for o in (await s.scalars(select(Outline).where(Outline.project_id == project_id).order_by(Outline.chapter_number))).all()
            ],
        }


@app.put("/api/projects/{project_id}/spec")
async def update_spec(project_id: str, data: SpecUpdate):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        if p.status not in ("paused_after_analyze", "queued", "failed", "cancelled", "needs_review"):
            raise HTTPException(409, "Chỉ cập nhật spec khi đang tạm dừng sau phân tích")
        if data.chapter_count is not None:
            p.options["chapter_count"] = data.chapter_count
        if data.target_words is not None:
            p.options["target_words"] = data.target_words
        if data.mode is not None:
            p.options["mode"] = data.mode
        # Update spec if exists
        if p.spec:
            p.spec.update({k: v for k, v in data.model_dump(exclude_none=True).items()})
        await s.flush()
        return asdict(p)


@app.put("/api/projects/{project_id}/bible")
async def update_bible(project_id: str, data: BibleUpdate):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        if p.status not in ("paused_after_bible", "queued", "failed", "cancelled", "needs_review"):
            raise HTTPException(409, "Chỉ cập nhật nhân vật khi đang tạm dừng sau xây dựng thế giới")
        # Clear existing characters
        from sqlalchemy import delete
        await s.execute(delete(Character).where(Character.project_id == project_id))
        # Add new characters
        for c in data.characters:
            s.add(Character(project_id=p.id, **c))
        p.bible = {**p.bible, "characters": data.characters} if p.bible else {"characters": data.characters}
        await s.flush()
        return asdict(p)


@app.put("/api/projects/{project_id}/outline")
async def update_outline(project_id: str, data: OutlineUpdate):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        if p.status not in ("paused_after_outline", "queued", "failed", "cancelled", "needs_review"):
            raise HTTPException(409, "Chỉ cập nhật dàn ý khi đang tạm dừng sau dàn ý")
        from sqlalchemy import delete
        await s.execute(delete(Outline).where(Outline.project_id == project_id))
        for item in data.chapters:
            s.add(Outline(project_id=p.id, **item))
        await s.flush()
        return asdict(p)


@app.post("/api/projects/{project_id}/resume")
async def resume_project(project_id: str):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
        if not job:
            raise HTTPException(409, "Không tìm thấy job")
        if not p.pause_stage:
            raise HTTPException(409, "Truyện không ở trạng thái tạm dừng")
        if job.status in ("queued", "running"):
            raise HTTPException(409, "Truyện đang được xử lý")
        # Clear pause and resume
        p.pause_stage = None
        p.status = "queued"
        job.status = "queued"
        job.generation += 1
        job.error = None
        event(s, p.id, job.id, "queued", resume=True)
        await s.flush()
        await s.refresh(job)
    await dispatch(job)
    return {"status": "queued", "generation": job.generation}


@app.post("/api/projects/{project_id}/resume-next-chapter")
async def resume_next_chapter(project_id: str):
    async with Session() as s, s.begin():
        p = await must_project(s, project_id, True)
        job = await s.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
        if not job:
            raise HTTPException(409, "Không tìm thấy job")
        if not p.pause_stage or not p.pause_stage.startswith("write:"):
            raise HTTPException(409, "Chỉ tiếp tục chương khi đang tạm dừng sau viết chương")
        if job.status in ("queued", "running"):
            raise HTTPException(409, "Truyện đang được xử lý")
        # Clear pause and resume - will continue from next_chapter
        p.pause_stage = None
        p.status = "queued"
        job.status = "queued"
        job.generation += 1
        job.error = None
        event(s, p.id, job.id, "queued", resume=True, next_chapter=p.next_chapter)
        await s.flush()
        await s.refresh(job)
    await dispatch(job)
    return {"status": "queued", "generation": job.generation, "next_chapter": p.next_chapter}
