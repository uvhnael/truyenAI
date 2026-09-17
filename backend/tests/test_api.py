"""API integration over ASGI + sqlite. Lifespan/Redis deliberately not started."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent.llm import LLM
from app.agent.workflow import run_story
from app.models import Event, Job, Project, Revision, Step


@pytest.fixture(autouse=True)
def fake_dispatch(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr("app.main.dispatch", dispatch)
    return dispatch


async def create(client, count=1):
    response = await client.post(
        "/api/projects",
        json={
            "title": "  Truyện kiểm thử  ",
            "prompt": "Một câu chuyện tiếng Việt về chiếc đèn.",
            "options": {"chapter_count": count, "target_words": 800},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def complete(client, db):
    pid = await create(client)
    await run_story(pid, 1)
    response = await client.get(f"/api/projects/{pid}")
    assert response.json()["status"] == "completed", response.text
    return pid, response.json()["chapters"][0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": "short"},
        {"prompt": " " * 12},
        {"prompt": "x" * 20001},
        {"title": "", "prompt": "valid prompt here"},
        {"title": "   ", "prompt": "valid prompt here"},
        {"title": "x" * 201, "prompt": "valid prompt here"},
        {"prompt": "valid prompt here", "options": {"chapter_count": 0}},
        {"prompt": "valid prompt here", "options": {"chapter_count": 31}},
        {"prompt": "valid prompt here", "options": {"target_words": 199}},
        {"prompt": "valid prompt here", "options": {"target_words": 5001}},
    ],
)
async def test_create_validation_no_database_side_effects(client, db, payload, fake_dispatch):
    response = await client.post("/api/projects", json=payload)
    assert response.status_code == 422
    async with db() as s:
        assert (await s.scalars(select(Project))).all() == []
        assert (await s.scalars(select(Job))).all() == []
        assert (await s.scalars(select(Event))).all() == []
    fake_dispatch.assert_not_awaited()


async def test_create_read_and_run_conflict(client, db, mock_mode, fake_dispatch):
    pid = await create(client)
    detail = (await client.get(f"/api/projects/{pid}")).json()
    assert detail["title"] == "Truyện kiểm thử"
    assert detail["status"] == detail["job"]["status"] == "queued"
    assert detail["job"]["generation"] == 1
    assert detail["options"] == {"chapter_count": 1, "target_words": 800}
    assert detail["chapters"] == detail["outline"] == detail["characters"] == []
    assert (await client.get("/api/projects")).json()[0]["id"] == pid
    assert (await client.post(f"/api/projects/{pid}/run")).status_code == 409
    fake_dispatch.assert_awaited_once()


async def test_omitted_options_remain_unspecified(client):
    response = await client.post("/api/projects", json={"prompt": "Một truyện đủ dài để kiểm thử"})
    assert response.status_code == 201
    assert response.json()["options"] == {}


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/missing",
        "/api/projects/missing/chapters",
        "/api/projects/missing/chapters/1",
        "/api/projects/missing/chapters/1/revisions",
        "/api/projects/missing/export",
        "/api/projects/missing/events",
    ],
)
async def test_get_missing_is_404(client, path):
    assert (await client.get(path)).status_code == 404


async def test_manual_edit_optimistic_concurrency_and_revision_history(client, db, mock_mode):
    pid, chapter = await complete(client, db)
    url = f"/api/projects/{pid}/chapters/1"
    edit = {
        "title": "  Tiêu đề sửa  ",
        "content": "Nội dung do người dùng chỉnh sửa thủ công.",
        "version": chapter["version"],
    }
    response = await client.put(url, json=edit)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == chapter["version"] + 1
    assert updated["title"] == "Tiêu đề sửa" and updated["content"] == edit["content"]
    assert updated["word_count"] == len(edit["content"].split())
    assert updated["status"] == "edited"
    assert updated["memory"] == {} and updated["summary"] == ""
    committed = (await client.get(url)).json()
    assert committed["version"] == updated["version"]
    assert committed["content"] == updated["content"]
    assert (await client.put(url, json=edit)).status_code == 409
    assert (await client.get(url)).json() == committed

    history = (await client.get(url + "/revisions")).json()
    assert [r["revision_no"] for r in history] == [3, 2, 1]
    assert [r["source"] for r in history] == ["manual", "ai_revision", "draft"]
    assert history[0]["content"] == edit["content"]
    assert history[1]["content"] == chapter["content"]
    detail = (await client.get(f"/api/projects/{pid}")).json()
    assert detail["status"] == "needs_review"
    assert detail["final_review"]["verdict"] == "revise"
    async with db() as s:
        edits = (await s.scalars(select(Event).where(Event.type == "chapter_edited"))).all()
        assert len(edits) == 1 and edits[0].payload["version"] == 3
    # Stale clients cannot append a revision; a fresh version can.
    edit["version"] = updated["version"]
    assert (await client.put(url, json=edit)).json()["version"] == 4


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
async def test_edit_rejected_before_job_done_without_mutation(client, db, mock_mode, status):
    pid, chapter = await complete(client, db)
    async with db() as s, s.begin():
        job = await s.scalar(select(Job).where(Job.project_id == pid))
        job.status = status
    response = await client.put(
        f"/api/projects/{pid}/chapters/1",
        json={
            "title": "Changed",
            "content": "Must not be saved",
            "version": chapter["version"],
        },
    )
    assert response.status_code == 409
    assert (await client.get(f"/api/projects/{pid}/chapters/1")).json() == chapter
    async with db() as s:
        assert len((await s.scalars(select(Revision))).all()) == 2
        assert (await s.scalar(select(Project).where(Project.id == pid))).status == "completed"


@pytest.mark.parametrize(
    "changes",
    [
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 100001},
        {"title": ""},
        {"title": "  "},
        {"title": "x" * 301},
        {"version": 0},
    ],
)
async def test_edit_validation(client, db, mock_mode, changes):
    pid, chapter = await complete(client, db)
    data = {"title": "Valid", "content": "Valid content", "version": chapter["version"], **changes}
    response = await client.put(f"/api/projects/{pid}/chapters/1", json=data)
    assert response.status_code == 422
    assert (await client.get(f"/api/projects/{pid}/chapters/1")).json() == chapter


async def test_cancel_retry_fences_generation_and_is_idempotent(client, db, mock_mode, fake_dispatch):
    pid = await create(client)
    url = f"/api/projects/{pid}"
    assert (await client.post(url + "/cancel")).json()["status"] == "cancelled"
    assert (await client.post(url + "/cancel")).json()["status"] == "cancelled"
    detail = (await client.get(url)).json()
    assert detail["job"]["generation"] == 2
    async with db() as s:
        assert len((await s.scalars(select(Event).where(Event.type == "job_cancelled"))).all()) == 1
    retry = await client.post(url + "/run")
    assert retry.status_code == 200
    assert retry.json()["generation"] == 3 and retry.json()["status"] == "queued"
    await run_story(pid, 1)  # stale delivery after retry
    assert (await client.get(url)).json()["spec"] is None
    await run_story(pid, 3)
    assert (await client.get(url)).json()["status"] == "completed"
    assert (await client.post(url + "/run")).status_code == 409
    fake_dispatch.assert_awaited()
    assert fake_dispatch.await_count == 2


async def test_api_cancel_rejects_inflight_provider_result(client, db, mock_mode):
    pid = await create(client)
    entered, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def call(self, task, ctx):
            entered.set()
            await release.wait()
            return await LLM().call(task, ctx)

    worker = asyncio.create_task(run_story(pid, 1, Delayed()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert (await client.post(f"/api/projects/{pid}/cancel")).status_code == 200
        release.set()
        await asyncio.wait_for(worker, 3)
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    detail = (await client.get(f"/api/projects/{pid}")).json()
    assert detail["status"] == detail["job"]["status"] == "cancelled"
    assert detail["spec"] is None
    async with db() as s:
        assert (await s.scalars(select(Step))).all() == []


async def test_export_contains_ordered_chapters_and_manual_edits(client, db, mock_mode):
    pid = await create(client, count=2)
    await run_story(pid, 1)
    response = await client.get(f"/api/projects/{pid}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == f'attachment; filename="story-{pid}.md"'
    assert response.text.startswith("# Truyện kiểm thử")
    assert response.text.index("## Chương 1") < response.text.index("## Chương 2")
    chapters = (await client.get(f"/api/projects/{pid}/chapters")).json()
    assert all(c["content"] in response.text for c in chapters)


async def test_events_rejects_bad_cursor_before_streaming(client, mock_mode):
    pid = await create(client)
    assert (
        await client.get(f"/api/projects/{pid}/events", headers={"Last-Event-ID": "not-an-int"})
    ).status_code == 400
    assert (await client.get(f"/api/projects/{pid}/events?after=-1")).status_code == 422
