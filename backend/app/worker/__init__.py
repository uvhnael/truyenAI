"""Redis delivery with PostgreSQL as source of truth and advisory execution locks."""

import hashlib
import time

from arq import Retry, cron
from sqlalchemy import select, text
from sqlalchemy import text as text_sql

from ..agent.publish import run_publish
from ..agent.tts import run_audio
from ..agent.video import run_video
from ..agent.workflow import run_story
from ..db import Session, engine
from ..migrate import migrate
from ..models import Chapter, Job, Publish, Video
from ..queue import enqueue, redis_settings


async def startup(ctx):
    await migrate()
    await recover_jobs(ctx)


async def recover_jobs(ctx):
    # Transactional outbox equivalent: queued/running DB jobs are periodically
    # redispatched. Redis job IDs deduplicate live tasks; no gap after a crash.
    async with Session() as s:
        jobs = (await s.scalars(select(Job).where(Job.status.in_(["queued", "running"])))).all()
        for job in jobs:
            await enqueue(ctx["redis"], job)
        became = f"audio:{int(time.time()) // 60}"
        await _reschedule_audio(ctx, s, lock=772519005, stamp=became)
        await _reschedule_videos(ctx, lock=772519006)
        await _reschedule_publishes(ctx, lock=772519007)


async def _reschedule_audio(ctx, s, lock, stamp):
    # A crashed arq task can leave a long in-progress marker. Recovery uses a new
    # delivery ID only while no TTS runner holds the PostgreSQL lock.
    async with engine.connect() as conn:
        free = await conn.scalar(text_sql("SELECT pg_try_advisory_lock(:k)"), {"k": lock})
        await conn.commit()
        if not free:
            return
        try:
            stuck = (await s.scalars(select(Chapter).where(
                Chapter.audio_status.in_(["queued", "processing"])))).all()
            for chapter in stuck:
                await ctx["redis"].enqueue_job(
                    "synthesize_chapter_task", chapter.project_id, chapter.chapter_number,
                    chapter.audio_generation,
                    _job_id=f"audio:{chapter.id}:{chapter.audio_generation}:recovery:{stamp}",
                )
        finally:
            await conn.execute(text_sql("SELECT pg_advisory_unlock(:k)"), {"k": lock})
            await conn.commit()


async def _reschedule_videos(ctx, lock):
    async with Session() as s2:
        stuck = (await s2.scalars(select(Video).where(
            Video.status.in_(["queued", "processing"])))).all()
        for video in stuck:
            await ctx["redis"].enqueue_job(
                "render_video_task", video.project_id, video.chapter_number, video.generation,
                _job_id=f"video:{video.id}:{video.generation}:recovery:{int(time.time()) // 60}",
            )


async def _reschedule_publishes(ctx, lock):
    async with Session() as s2:
        stuck = (await s2.scalars(select(Publish).where(
            Publish.status.in_(["queued", "uploading", "processing"])))).all()
        for publish in stuck:
            await ctx["redis"].enqueue_job(
                "publish_task", publish.id, publish.generation,
                _job_id=f"publish:{publish.id}:recovery:{int(time.time()) // 60}",
            )


async def synthesize_chapter_task(ctx, project_id: str, chapter_number: int, generation: int | None = None):
    if generation is None:  # obsolete pre-versioned delivery
        return
    async with engine.connect() as conn:
        acquired = await conn.scalar(text_sql("SELECT pg_try_advisory_lock(772519005)"))
        await conn.commit()
        if not acquired:
            raise Retry(defer=15)
        try:
            await run_audio(project_id, chapter_number, generation)
        finally:
            await conn.execute(text_sql("SELECT pg_advisory_unlock(772519005)"))
            await conn.commit()


async def generate_story(ctx, project_id: str, generation: int):
    key = int.from_bytes(hashlib.sha256(project_id.encode()).digest()[:8], "big", signed=True)
    async with engine.connect() as conn:
        acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await conn.commit()
        if not acquired:
            raise Retry(defer=10)
        try:
            await run_story(project_id, generation)
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await conn.commit()


async def render_video_task(ctx, project_id: str, chapter_number: int, generation: int):
    async with engine.connect() as conn:
        acquired = await conn.scalar(text_sql("SELECT pg_try_advisory_lock(772519006)"))
        await conn.commit()
        if not acquired:
            raise Retry(defer=10)
        try:
            await run_video(project_id, chapter_number, generation)
        finally:
            await conn.execute(text_sql("SELECT pg_advisory_unlock(772519006)"))
            await conn.commit()


async def publish_task(ctx, publish_id: str, generation: int):
    async with engine.connect() as conn:
        acquired = await conn.scalar(text_sql("SELECT pg_try_advisory_lock(772519007)"))
        await conn.commit()
        if not acquired:
            raise Retry(defer=10)
        try:
            await run_publish(publish_id, generation)
        finally:
            await conn.execute(text_sql("SELECT pg_advisory_unlock(772519007)"))
            await conn.commit()


class WorkerSettings:
    functions = [generate_story, synthesize_chapter_task, render_video_task, publish_task]
    redis_settings = redis_settings
    on_startup = startup
    cron_jobs = [cron(recover_jobs, second={0, 15, 30, 45}, run_at_startup=True)]
    max_jobs = 2
    job_timeout = 7200
    keep_result = 0
    max_tries = 100
    health_check_interval = 15
