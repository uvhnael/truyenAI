"""Versioned baseline migration with a PostgreSQL advisory lock.

Future migrations must be added explicitly; create_all does NOT alter existing columns.
"""

import asyncio

from sqlalchemy import text

from .db import engine
from .models import Base


async def migrate():
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(772519004)"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
        version = await conn.scalar(text("SELECT version FROM schema_migrations WHERE version=1"))
        if version is None:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (1)"))


if __name__ == "__main__":
    asyncio.run(migrate())
