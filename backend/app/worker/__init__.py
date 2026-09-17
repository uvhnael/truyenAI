"""Redis delivery with PostgreSQL as source of truth and advisory execution locks."""

import hashlib

from arq import Retry, cron
from sqlalchemy import select, text

from ..agent.workflow import run_story
from ..db import Session, engine
from ..migrate import migrate
from ..models import Job
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


class WorkerSettings:
    functions = [generate_story]
    redis_settings = redis_settings
    on_startup = startup
    cron_jobs = [cron(recover_jobs, second={0, 15, 30, 45}, run_at_startup=True)]
    max_jobs = 2
    job_timeout = 7200
    keep_result = 0
    max_tries = 100
    health_check_interval = 15
