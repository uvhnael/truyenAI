"""Truyện ngắn: đúng 1 chương bất kể LLM/prompt nói gì."""
from sqlalchemy import select

from app.agent.workflow import run_story
from app.models import Job, Outline, Project


async def test_short_mode_forces_single_chapter(db, mock_mode):
    async with db() as s, s.begin():
        p = Project(
            title="Truyện ngắn",
            prompt="Truyện ngắn về An, 5 chương nhé.",
            options={"mode": "short"},
            status="queued",
        )
        s.add(p)
        await s.flush()
        s.add(Job(project_id=p.id, status="queued", generation=1))
        pid = p.id
    await run_story(pid, 1)
    async with db() as s:
        outlines = (await s.scalars(select(Outline).where(Outline.project_id == pid))).all()
        project = await s.get(Project, pid)
    assert project.spec["chapter_count"] == 1
    assert project.spec["mode"] == "short"
    assert [o.chapter_number for o in outlines] == [1]
