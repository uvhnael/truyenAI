"""Shared fixtures: sqlite in-memory async engine, patched Session everywhere, HTTP client."""

import importlib.util
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.db as db_module  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_provider_settings(monkeypatch):
    """Never inherit a developer's provider key or accidentally make live requests."""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_mode", "mock")


@pytest.fixture
def mock_mode(monkeypatch):
    """Force explicit mock mode (never auto)."""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_mode", "mock")
    return settings


def _patch_sessions(monkeypatch, session_factory):
    """Patch every Session entry point the app may use."""
    import app.main as main_module

    monkeypatch.setattr(db_module, "Session", session_factory)
    if hasattr(main_module, "Session"):
        monkeypatch.setattr(main_module, "Session", session_factory)
    if importlib.util.find_spec("app.agent.workflow") is not None:
        import app.agent.workflow as wf

        if hasattr(wf, "Session"):
            monkeypatch.setattr(wf, "Session", session_factory)


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory, monkeypatch):
    """Patch app Session symbols to the in-memory factory; returns the factory."""
    _patch_sessions(monkeypatch, session_factory)
    return session_factory


@pytest_asyncio.fixture
async def client(db):
    """HTTPX client over the FastAPI app without running lifespan."""
    import app.main as main_module

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
