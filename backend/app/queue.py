from arq.connections import RedisSettings

from .config import settings

redis_settings = RedisSettings.from_dsn(settings.redis_url)


async def enqueue(redis, job):
    return await redis.enqueue_job(
        "generate_story", job.project_id, job.generation, _job_id=f"story:{job.id}:{job.generation}"
    )
