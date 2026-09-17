"""Durable workflow integration tests; no Redis or remote LLM required."""

import asyncio
from collections import Counter

import pytest
from sqlalchemy import select

from app.agent.llm import LLM, LLMError
from app.agent.workflow import StoryAgent, run_story
from app.models import Chapter, Character, Event, Job, Outline, Project, Revision, Step


async def seed(db, count=2, generation=1, status="queued"):
    async with db() as s, s.begin():
        p = Project(
            title="Kiểm thử",
            prompt="Một truyện tiếng Việt về An và chiếc đèn.",
            options={"chapter_count": count, "target_words": 800},
            status=status,
        )
        s.add(p)
        await s.flush()
        j = Job(project_id=p.id, status=status, generation=generation)
        s.add(j)
        await s.flush()
        return p.id, j.id


async def rows(db, model):
    async with db() as s:
        return (await s.scalars(select(model))).all()


class RecordingLLM:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def call(self, task, context):
        self.calls.append((task, context))
        if self.fail_at and self.fail_at(task, context):
            raise LLMError("Injected provider failure")
        return await LLM().call(task, context)


async def test_complete_mock_pipeline_revisions_memories_events(db, mock_mode):
    pid, jid = await seed(db)
    llm = RecordingLLM()
    await run_story(pid, 1, llm)
    project = (await rows(db, Project))[0]
    job = (await rows(db, Job))[0]
    assert project.status == "completed"
    assert job.status == "done" and job.error is None
    assert project.spec["chapter_count"] == 2
    assert project.bible["characters"]
    assert project.final_review["verdict"] == "pass"
    assert len(await rows(db, Character)) == 1
    assert sorted(o.chapter_number for o in await rows(db, Outline)) == [1, 2]
    chapters = sorted(await rows(db, Chapter), key=lambda c: c.chapter_number)
    revisions = await rows(db, Revision)
    assert len(chapters) == 2 and len(revisions) == 4
    for c in chapters:
        assert c.status == "final" and c.version == 2
        assert c.word_count == len(c.content.split())
        assert "BẢN DEMO" in c.content
        assert c.summary == c.memory["summary"]
        history = sorted((r for r in revisions if r.chapter_id == c.id), key=lambda r: r.revision_no)
        assert [r.source for r in history] == ["draft", "ai_revision"]
        assert [r.revision_no for r in history] == [1, 2]
        assert history[0].content != history[1].content == c.content
        assert history[1].review["verdict"] == "revise"
    assert chapters[-1].memory["open_threads"] == []
    writes = [ctx for task, ctx in llm.calls if task == "write"]
    assert writes[0]["memory"] == []
    assert writes[1]["memory"][0]["summary"] == chapters[0].summary
    steps = await rows(db, Step)
    assert len(steps) == 16
    assert len({s.key for s in steps}) == 16
    events = await rows(db, Event)
    assert Counter(e.type for e in events) == {
        "stage_started": 14,
        "stage_completed": 14,
        "chapter_done": 2,
        "job_done": 1,
    }
    assert all(e.project_id == pid and e.job_id == jid for e in events)
    assert next(e for e in events if e.type == "job_done").payload["mode"] == "mock"
    # Redelivering a completed generation must not call the provider or add rows.
    await run_story(pid, 1, llm)
    assert len(llm.calls) == 14
    assert len(await rows(db, Event)) == len(events)


async def test_failed_provider_resume_preserves_checkpoints_and_revisions(db, mock_mode):
    pid, jid = await seed(db)
    failing = RecordingLLM(
        fail_at=lambda task, ctx: task == "memory" and ctx["chapter"]["chapter_number"] == 1
    )
    await run_story(pid, 1, failing)
    job = (await rows(db, Job))[0]
    assert job.status == "failed" and job.stage == "memory:1"
    assert job.error == "Injected provider failure"
    before_steps = {s.key: (s.id, s.output) for s in await rows(db, Step)}
    assert set(before_steps) == {
        "analyze",
        "outline",
        "bible",
        "write:1",
        "review:1:0",
        "revise:1:0",
        "review:1:1",
    }
    before_revisions = {r.id: r.content for r in await rows(db, Revision)}
    assert len(before_revisions) == 2
    async with db() as s, s.begin():
        job = await s.get(Job, jid)
        job.status, job.generation = "queued", 2
        (await s.get(Project, pid)).status = "queued"
    resumed = RecordingLLM()
    await run_story(pid, 2, resumed)
    assert (await rows(db, Project))[0].status == "completed"
    assert resumed.calls[0][0] == "memory"
    assert len(resumed.calls) == 7
    after_steps = {s.key: (s.id, s.output) for s in await rows(db, Step)}
    assert all(after_steps[key] == value for key, value in before_steps.items())
    after_revisions = {r.id: r.content for r in await rows(db, Revision)}
    assert all(after_revisions[key] == value for key, value in before_revisions.items())
    assert len(after_revisions) == 4
    assert len(await rows(db, Outline)) == 2
    assert len(await rows(db, Character)) == 1


@pytest.mark.parametrize("status,generation", [("cancelled", 1), ("queued", 2), ("done", 1)])
async def test_stopped_or_stale_job_never_calls_llm(db, mock_mode, status, generation):
    pid, _ = await seed(db, status=status, generation=generation)
    llm = RecordingLLM()
    await run_story(pid, 1, llm)
    assert llm.calls == []
    assert await rows(db, Step) == []
    assert await rows(db, Event) == []
    assert (await rows(db, Project))[0].status == status


@pytest.mark.parametrize("new_status", ["cancelled", "queued"])
async def test_inflight_result_is_fenced_after_cancellation_or_new_generation(db, mock_mode, new_status):
    pid, jid = await seed(db)
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockedLLM:
        async def call(self, task, context):
            entered.set()
            await release.wait()
            return await LLM().call(task, context)

    task = asyncio.create_task(run_story(pid, 1, BlockedLLM()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with db() as s, s.begin():
            j = await s.get(Job, jid)
            j.generation, j.status = 2, new_status
            (await s.get(Project, pid)).status = new_status
        release.set()
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (await rows(db, Project))[0].spec is None
    assert (await rows(db, Job))[0].status == new_status
    assert await rows(db, Step) == []
    assert [e.type for e in await rows(db, Event)] == ["stage_started"]


async def test_apply_and_checkpoint_rollback_atomically(db, mock_mode):
    pid, _ = await seed(db)
    agent = StoryAgent(pid, 1)

    async def broken_apply(s, p, data):
        p.spec = data
        await s.flush()
        raise ValueError("fail applying output")

    with pytest.raises(ValueError, match="fail applying"):
        await agent.step("analyze", "analyze", {"prompt": "A story"}, broken_apply)
    assert (await rows(db, Project))[0].spec is None
    assert await rows(db, Step) == []
    assert [e.type for e in await rows(db, Event)] == ["stage_started"]


async def test_step_checkpoint_short_circuits_provider_and_effects(db, mock_mode):
    pid, _ = await seed(db)
    llm = RecordingLLM()
    applied = []

    async def apply(s, p, output):
        applied.append(output)
        p.spec = output

    agent = StoryAgent(pid, 1, llm)
    first = await agent.step("analyze", "analyze", {"prompt": "first"}, apply)
    again = await agent.step("analyze", "analyze", {"prompt": "different"}, apply)
    assert first == again
    assert len(applied) == len(llm.calls) == len(await rows(db, Step)) == 1


async def test_invalid_outline_fails_without_partial_outline_checkpoint(db, mock_mode):
    pid, _ = await seed(db)

    class InvalidOutline:
        async def call(self, task, ctx):
            data = await LLM().call(task, ctx)
            if task == "outline":
                data["chapters"][1]["chapter_number"] = 1
            return data

    await run_story(pid, 1, InvalidOutline())
    assert (await rows(db, Job))[0].status == "failed"
    assert await rows(db, Outline) == []
    assert [s.key for s in await rows(db, Step)] == ["analyze"]


async def test_unresolved_major_issues_never_mark_story_completed(db, mock_mode, monkeypatch):
    monkeypatch.setattr(mock_mode, "max_revision_rounds", 1)
    pid, _ = await seed(db, count=1)
    calls = []

    class NeverPass:
        async def call(self, task, ctx):
            calls.append(task)
            if task in ("review", "final_review"):
                return {
                    "verdict": "pass",
                    "issues": [{"chapter_number": 1, "severity": "major", "description": "Continuity error"}],
                    "notes": "Unresolved",
                }
            return await LLM().call(task, ctx)

    await run_story(pid, 1, NeverPass())
    assert (await rows(db, Project))[0].status == "needs_review"
    assert (await rows(db, Job))[0].status == "done"
    assert calls.count("revise") == 2  # one local, one global; bounded
    assert calls.count("final_review") == 2
    assert len(await rows(db, Revision)) == 3
    assert (await rows(db, Chapter))[0].status == "needs_review"


async def test_global_repair_rechecks_chapter_and_can_resolve_local_failure(db, mock_mode, monkeypatch):
    monkeypatch.setattr(mock_mode, "max_revision_rounds", 1)
    pid, _ = await seed(db, count=1)
    repair_started = False
    final_calls = 0

    class RepairingLLM:
        async def call(self, task, ctx):
            nonlocal repair_started, final_calls
            issue = {"chapter_number": 1, "severity": "major", "description": "Repair ending"}
            if task == "final_review":
                final_calls += 1
                if final_calls == 1:
                    repair_started = True
                    return {"verdict": "revise", "issues": [issue], "notes": "Global repair needed"}
                return {"verdict": "pass", "issues": [], "notes": "Resolved"}
            if task == "review":
                return {
                    "verdict": "pass" if repair_started else "revise",
                    "issues": [] if repair_started else [issue],
                    "notes": "Checked",
                }
            return await LLM().call(task, ctx)

    await run_story(pid, 1, RepairingLLM())
    assert (await rows(db, Project))[0].status == "completed"
    chapter = (await rows(db, Chapter))[0]
    assert chapter.status == "final" and chapter.version == 3
    steps = {step.key: step.output for step in await rows(db, Step)}
    assert steps["final_recheck:0:1"]["verdict"] == "pass"
    assert chapter.summary == steps["final_memory:0:1"]["summary"]
    assert final_calls == 2
