"""Resume between a global repair and story finalization must not regress statuses."""
from sqlalchemy import select

from app.agent.llm import LLM
from app.agent.workflow import run_story
from app.models import Chapter, Job, Project, Revision, Step


class PolicyLLM:
    """Local reviews stay unresolved; only post-repair rechecks and the global pass are clean."""

    def __init__(self, fail_when=None):
        self.fail_when = fail_when or (lambda task, ctx: False)
        self.calls = []

    async def call(self, task, ctx):
        self.calls.append(task)
        if self.fail_when(task, ctx):
            raise RuntimeError("injected stop")
        if task == "review":
            clean = bool(ctx.get("recheck"))
            return {
                "verdict": "pass" if clean else "revise",
                "issues": [] if clean else [
                    {"chapter_number": ctx["chapter"]["chapter_number"], "severity": "major",
                     "description": "Chưa đạt yêu cầu"},
                ],
                "notes": "",
            }
        if task == "final_review":
            if ctx.get("round") == 0:
                return {"verdict": "revise", "issues": [
                    {"chapter_number": ctx["spec"]["chapter_count"], "severity": "major",
                     "description": "Kết chương chưa chặt"},
                ], "notes": ""}
            return {"verdict": "pass", "issues": [], "notes": ""}
        return await LLM().call(task, ctx)


async def _scalar_db_job(s, pid):
    from sqlalchemy import select as _sel
    return await s.scalar(_sel(Job).where(Job.project_id == pid))


async def _scalar(db, model, **filters):
    async with db() as s:
        return await s.scalar(select(model).filter_by(**filters))


async def test_resume_preserves_rechecked_repair_status(db, mock_mode, monkeypatch):
    monkeypatch.setattr(mock_mode, "max_revision_rounds", 1)
    async with db() as s, s.begin():
        p = Project(title="Resume", prompt="Một truyện tiếng Việt đủ dài.",
                    options={"chapter_count": 1}, status="queued")
        s.add(p)
        await s.flush()
        s.add(Job(project_id=p.id, status="queued", generation=1))
        pid = p.id

    # Round-1 local review stays unresolved; only the final recheck can clear it.
    first = PolicyLLM(fail_when=lambda task, ctx: task == "final_review" and ctx.get("round") == 1)

    await run_story(pid, 1, first)
    async with db() as s:
        keys = [x.key for x in (await s.scalars(select(Step))).all()]
    assert any(key.startswith("final_recheck:") for key in keys)
    assert (await _scalar(db, Chapter, project_id=pid, chapter_number=1)).status == "final"
    async with db() as s:
        job = await _scalar(db, Job, project_id=pid)
        assert job.status == "failed"
        revisions_before = len((await s.scalars(select(Revision))).all())

    # Resume: cached final_revise/final_recheck must not re-run, duplicate revisions,
    # or flip the rechecked chapter back to an older state.
    async with db() as s, s.begin():
        job = await _scalar_db_job(s, pid)
        job.status, job.generation = "queued", 2
        (await s.get(Project, pid)).status = "queued"
    await run_story(pid, 2, PolicyLLM())
    assert (await _scalar(db, Chapter, project_id=pid, chapter_number=1)).status == "final"
    assert (await _scalar(db, Project, id=pid)).status == "completed"
    async with db() as s:
        assert len((await s.scalars(select(Revision))).all()) == revisions_before
        keys = [x.key for x in (await s.scalars(select(Step))).all()]
    assert len(keys) == len(set(keys))
