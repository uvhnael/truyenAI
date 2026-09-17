import asyncio
import json
from contextlib import asynccontextmanager
from typing import Annotated

from arq import create_pool
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from .agent.workflow import asdict, chapter_row, event, save_revision
from .config import settings
from .db import Session
from .migrate import migrate
from .models import Chapter, Character, Event, Job, Outline, Project, Revision
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


async def must_chapter(s, project_id, n):
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
