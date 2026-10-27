"""Sequential idempotent migrations serialized across API and workers."""
import asyncio

from sqlalchemy import text

from .db import engine
from .models import Base


async def migrate():
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(772519004)"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
        version = await conn.scalar(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))
        if version < 1:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (1)"))
        if version < 2:
            for column, ddl in [
                ("audio_status", "VARCHAR(24) NOT NULL DEFAULT 'none'"),
                ("audio_path", "TEXT"), ("audio_voice", "VARCHAR(64)"),
                ("audio_seconds", "DOUBLE PRECISION"),
            ]:
                await conn.execute(text(f"ALTER TABLE chapters ADD COLUMN IF NOT EXISTS {column} {ddl}"))
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (2)"))
        if version < 3:
            for column, ddl in [
                ("audio_generation", "INTEGER NOT NULL DEFAULT 0"),
                ("audio_version", "INTEGER"), ("audio_error", "TEXT"),
            ]:
                await conn.execute(text(f"ALTER TABLE chapters ADD COLUMN IF NOT EXISTS {column} {ddl}"))
            # Existing unfinished attempts have no version/voice snapshot. Require
            # an explicit retry rather than guessing what text/voice they meant.
            await conn.execute(text("UPDATE chapters SET audio_status='failed', audio_error='Tác vụ cũ bị gián đoạn; hãy tổng hợp lại.' WHERE audio_status IN ('processing', 'queued') AND audio_version IS NULL"))
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (3)"))
        if version < 4:
            from .models import Publish, SocialAccount, Video
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Video.__table__, SocialAccount.__table__, Publish.__table__],
                checkfirst=True,
            ))
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (4)"))
        if version < 5:
            # Add pause_stage and next_chapter columns for interactive writing flow
            await conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS pause_stage VARCHAR(32)"))
            await conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS next_chapter INTEGER NOT NULL DEFAULT 1"))
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (5)"))


if __name__ == "__main__":
    asyncio.run(migrate())
