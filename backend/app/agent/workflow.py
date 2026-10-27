"""One agent, durable stages. A checkpoint and its effects commit together.

Only the worker owns the PostgreSQL advisory execution lock. Every write is also
fenced by job.generation, so cancellation/retry cannot accept stale LLM results.

Pause stages: analyze, outline, bible, write:N
"""

from sqlalchemy import select

from ..config import settings
from ..db import Session
from ..models import Chapter, Character, Event, Job, Outline, Project, Revision, Step
from .llm import LLM


class Stopped(Exception):
    pass


def asdict(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def event(session, project_id, job_id, kind, **payload):
    session.add(Event(project_id=project_id, job_id=job_id, type=kind, payload=payload))


async def locked(session, project_id, generation):
    # Consistent lock order: project before job (also used by HTTP endpoints).
    project = await session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    job = await session.scalar(select(Job).where(Job.project_id == project_id).with_for_update())
    if not project or not job or job.generation != generation or job.status not in ("queued", "running"):
        raise Stopped()
    return project, job


async def chapter_row(session, project_id, n):
    return await session.scalar(
        select(Chapter).where(Chapter.project_id == project_id, Chapter.chapter_number == n)
    )


async def save_revision(session, chapter, content, source, review=None):
    chapter.content = content
    chapter.word_count = len(content.split())
    chapter.version += 1
    session.add(
        Revision(
            chapter_id=chapter.id,
            revision_no=chapter.version,
            title=chapter.title,
            content=content,
            source=source,
            review=review or {},
        )
    )


async def context_for(project_id, n=None):
    async with Session() as s:
        p = await s.get(Project, project_id)
        outlines = (
            await s.scalars(
                select(Outline).where(Outline.project_id == project_id).order_by(Outline.chapter_number)
            )
        ).all()
        chapters = (
            await s.scalars(
                select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_number)
            )
        ).all()
        ctx = {
            "prompt": p.prompt,
            "options": p.options,
            "spec": p.spec,
            "bible": p.bible,
            "outline": [
                {"chapter_number": o.chapter_number, "title": o.title, "summary": o.summary, "beats": o.beats}
                for o in outlines
            ],
            "memory": [
                {"chapter_number": c.chapter_number, **c.memory}
                for c in chapters
                if n is None or c.chapter_number < n
            ],
        }
        if n is not None:
            o = next(x for x in outlines if x.chapter_number == n)
            ctx["chapter"] = {"chapter_number": n, "title": o.title, "summary": o.summary, "beats": o.beats}
            current = next((c for c in chapters if c.chapter_number == n), None)
            if current:
                ctx["content"] = current.content
                ctx["actual_word_count"] = current.word_count
        elif chapters:
            ctx["ending_content"] = chapters[-1].content
        return ctx


class StoryAgent:
    def __init__(self, project_id, generation, llm=None):
        self.project_id = project_id
        self.generation = generation
        self.llm = llm or LLM()

    async def _check_pause(self, s, p, stage):
        """Check if project is paused at this stage. Returns True if should pause."""
        if p.pause_stage == stage:
            p.status = f"paused_after_{stage}"
            await s.flush()
            return True
        return False

    async def _should_run_stage(self, s, p, stage):
        """Check if stage should run (not already completed and not paused before it)."""
        # Check if stage already has a checkpoint
        existing = await s.scalar(select(Step).where(Step.job_id == p.id, Step.key == stage))
        if existing:
            return False
        # Check if paused at an earlier stage
        pause_order = ["analyze", "outline", "bible"]
        if stage in pause_order and p.pause_stage:
            pause_idx = pause_order.index(p.pause_stage)
            stage_idx = pause_order.index(stage)
            if pause_idx < stage_idx:
                return False
        return True

    async def step(self, key, task, context, apply=None):
        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            previous = await s.scalar(select(Step).where(Step.job_id == job.id, Step.key == key))
            if previous:
                return previous.output
            job.status = p.status = "running"
            job.stage = key
            event(s, p.id, job.id, "stage_started", stage=key)
        output = await self.llm.call(task, context)
        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            # Defensive idempotency even when directly exercised without worker lock.
            previous = await s.scalar(select(Step).where(Step.job_id == job.id, Step.key == key))
            if previous:
                return previous.output
            if apply:
                await apply(s, p, output)
            s.add(Step(job_id=job.id, key=key, output=output))
            event(s, p.id, job.id, "stage_completed", stage=key)
        return output

    async def run(self):
        # Check if we should resume from a specific chapter
        async with Session() as s:
            p = await s.get(Project, self.project_id)
            if not p:
                return
            start_chapter = p.next_chapter if p.pause_stage and p.pause_stage.startswith("write:") else 1
        
        # Check if auto mode is enabled (run all stages without pausing)
        auto_mode = p.options.get("auto_mode", False)

        # Stage 1: Analyze (create spec)
        if await self._should_run_stage(*(await self._get_project_and_job())[:2], "analyze"):
            async def apply_spec(s, p, data):
                data.update({
                    k: v
                    for k, v in p.options.items()
                    if k in ("chapter_count", "target_words") and v is not None
                })
                data["mode"] = p.options.get("mode") or settings.default_mode
                if data["mode"] == "short":
                    data["chapter_count"] = 1
                p.spec = data

            await self.step("analyze", "analyze", await context_for(self.project_id), apply_spec)
            # Only pause if explicitly requested via pause_stage AND not in auto mode
            async with Session() as s, s.begin():
                p, job = await locked(s, self.project_id, self.generation)
                if not auto_mode and p.pause_stage == "analyze" and await self._check_pause(s, p, "analyze"):
                    return

        # Stage 2: Outline
        if await self._should_run_stage(*(await self._get_project_and_job())[:2], "outline"):
            async def apply_outline(s, p, data):
                count = p.spec["chapter_count"]
                if [c["chapter_number"] for c in data["chapters"]] != list(range(1, count + 1)):
                    raise ValueError("Outline phải có đúng số chương, đánh số liên tiếp từ 1.")
                for item in data["chapters"]:
                    s.add(Outline(project_id=p.id, **item))

            await self.step("outline", "outline", await context_for(self.project_id), apply_outline)
            async with Session() as s, s.begin():
                p, job = await locked(s, self.project_id, self.generation)
                if not auto_mode and p.pause_stage == "outline" and await self._check_pause(s, p, "outline"):
                    return

        # Stage 3: Bible/Characters
        if await self._should_run_stage(*(await self._get_project_and_job())[:2], "bible"):
            async def apply_bible(s, p, data):
                p.bible = data
                for c in data["characters"]:
                    s.add(Character(project_id=p.id, **c))

            await self.step("bible", "bible", await context_for(self.project_id), apply_bible)
            async with Session() as s, s.begin():
                p, job = await locked(s, self.project_id, self.generation)
                if not auto_mode and p.pause_stage == "bible" and await self._check_pause(s, p, "bible"):
                    return

        # Stage 4: Write chapters (starting from start_chapter)
        ctx = await context_for(self.project_id)
        if not ctx or "spec" not in ctx:
            return
        chapter_count = ctx["spec"]["chapter_count"]

        last_review = None
        for n in range(start_chapter, chapter_count + 1):
            # Update next_chapter for potential pause (only if pause was requested)
            async with Session() as s, s.begin():
                p, job = await locked(s, self.project_id, self.generation)
                p.next_chapter = n
                # Only set pause_stage for write if user explicitly requested pause at this chapter
                if p.pause_stage and p.pause_stage.startswith("write:") and int(p.pause_stage.split(":")[1]) == n:
                    pass  # Keep existing pause_stage
                else:
                    p.pause_stage = None  # Clear any auto-set pause_stage
                await s.flush()

            if await self._should_run_stage(*(await self._get_project_and_job())[:2], f"write:{n}"):
                async def apply_draft(s, p, data, number=n):
                    o = await s.scalar(
                        select(Outline).where(Outline.project_id == p.id, Outline.chapter_number == number)
                    )
                    c = Chapter(
                        project_id=p.id,
                        chapter_number=number,
                        title=o.title,
                        content="",
                        summary="",
                        memory={},
                        version=0,
                        word_count=0,
                        status="draft",
                    )
                    s.add(c)
                    await s.flush()
                    await save_revision(s, c, data["content"], "draft")

                await self.step(f"write:{n}", "write", await context_for(self.project_id, n), apply_draft)

                # Only pause after chapter if explicitly requested AND not in auto mode
                async with Session() as s, s.begin():
                    p, job = await locked(s, self.project_id, self.generation)
                    if not auto_mode and p.pause_stage == f"write:{n}" and await self._check_pause(s, p, f"write:{n}"):
                        return

                # Continue with review/revision loop for this chapter
                review = None
                for r in range(settings.max_revision_rounds + 1):
                    ctx = await context_for(self.project_id, n)
                    ctx["round"] = r
                    review = await self.step(f"review:{n}:{r}", "review", ctx)
                    if review["verdict"] == "pass" and not any(
                        i["severity"] == "major" for i in review["issues"]
                    ):
                        break
                    if r < settings.max_revision_rounds:
                        await self.revise(n, f"revise:{n}:{r}", review)
                await self.remember(n, f"memory:{n}")
                passed = review["verdict"] == "pass" and not any(
                    i["severity"] == "major" for i in review["issues"]
                )
                await self.settle_chapter(n, f"settle:{n}", passed)
                last_review = review

        # Clear pause_stage and next_chapter when all chapters done
        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            p.pause_stage = None
            p.next_chapter = 1

        # Whole-story final review (only if not short mode or has multiple chapters)
        if chapter_count > 1 or ctx["spec"].get("mode") != "short":
            report = None
            for r in range(settings.max_revision_rounds + 1):
                final_context = await context_for(self.project_id)
                final_context["round"] = r
                report = await self.step(f"final_review:{r}", "final_review", final_context)
                if report["verdict"] == "pass" and not any(i["severity"] == "major" for i in report["issues"]):
                    break
                if r == settings.max_revision_rounds:
                    break
                count = (await context_for(self.project_id))["spec"]["chapter_count"]
                numbers = sorted({i["chapter_number"] for i in report["issues"] if i["chapter_number"] <= count})
                for n in numbers:
                    await self.revise(n, f"final_revise:{r}:{n}", report)
                    await self.remember(n, f"final_memory:{r}:{n}")
                    check_context = await context_for(self.project_id, n)
                    check_context["round"] = settings.max_revision_rounds
                    check_context["recheck"] = True

                    async def apply_recheck(s, p, data, number=n):
                        chapter = await chapter_row(s, p.id, number)
                        passed = data["verdict"] == "pass" and not any(
                            issue["severity"] == "major" for issue in data["issues"]
                        )
                        chapter.status = "final" if passed else "needs_review"

                    await self.step(
                        f"final_recheck:{r}:{n}", "review", check_context, apply_recheck
                    )
        else:
            # Short mode: use last chapter review as final
            report = last_review if last_review else {"verdict": "pass", "issues": [], "notes": "Short mode complete"}

        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            unresolved = await s.scalar(
                select(Chapter).where(Chapter.project_id == p.id, Chapter.status == "needs_review")
            )
            p.final_review = report
            p.status = (
                "completed"
                if report["verdict"] == "pass"
                and not any(i["severity"] == "major" for i in report["issues"])
                and not unresolved
                else "needs_review"
            )
            job.status = "done"
            job.stage = "completed"
            job.error = None
            event(
                s,
                p.id,
                job.id,
                "job_done",
                status=p.status,
                review=report,
                mode="mock" if settings.mock else "live",
            )

    async def _get_project_and_job(self):
        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            return s, p, job

    async def revise(self, n, key, review):
        ctx = await context_for(self.project_id, n)
        ctx["review"] = review

        async def apply(s, p, data):
            c = await chapter_row(s, p.id, n)
            await save_revision(s, c, data["content"], "ai_revision", review)
            c.status = "revised"

        await self.step(key, "revise", ctx, apply)

    async def settle_chapter(self, n, key, passed):
        # Status writes are checkpointed too; otherwise a resume replays a stale
        # pre-repair review and flips a rechecked chapter back to its old state.
        status = "final" if passed else "needs_review"
        async with Session() as s, s.begin():
            p, job = await locked(s, self.project_id, self.generation)
            if await s.scalar(select(Step).where(Step.job_id == job.id, Step.key == key)):
                return
            (await chapter_row(s, p.id, n)).status = status
            s.add(Step(job_id=job.id, key=key, output={"chapter_number": n, "status": status}))
            event(s, p.id, job.id, "chapter_done", chapter_number=n, status=status)

    async def remember(self, n, key):
        async def apply(s, p, data):
            c = await chapter_row(s, p.id, n)
            c.summary, c.memory = data["summary"], data

        await self.step(key, "memory", await context_for(self.project_id, n), apply)


async def run_story(project_id, generation, llm=None):
    try:
        await StoryAgent(project_id, generation, llm).run()
    except Stopped:
        return
    except Exception as exc:
        from .llm import LLMError

        # Avoid persisting provider bodies, credentials, SQL parameters, or story data in errors.
        error = (
            str(exc)
            if isinstance(exc, (LLMError, ValueError))
            else f"{type(exc).__name__}: tác vụ thất bại, kiểm tra dịch vụ và thử lại."
        )
        async with Session() as s, s.begin():
            try:
                p, job = await locked(s, project_id, generation)
            except Stopped:
                return
            p.status = job.status = "failed"
            job.error = error[:1000]
            event(s, p.id, job.id, "job_failed", error=job.error, stage=job.stage)