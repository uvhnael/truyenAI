import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bible: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    # Pause control: which stage to resume from
    pause_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)  # analyze, outline, bible, write:N
    # Next chapter to generate (for chapter-by-chapter)
    next_chapter: Mapped[int] = mapped_column(Integer, default=1)


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    arc: Mapped[dict] = mapped_column(JSON)


class Outline(Base):
    __tablename__ = "outline"
    __table_args__ = (UniqueConstraint("project_id", "chapter_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    beats: Mapped[list] = mapped_column(JSON)


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("project_id", "chapter_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    memory: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    audio_status: Mapped[str] = mapped_column(String(24), default="none")
    audio_generation: Mapped[int] = mapped_column(Integer, default=0)
    audio_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_voice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_seconds: Mapped[float | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("project_id", "chapter_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer)
    audio_version: Mapped[int] = mapped_column(Integer)
    audio_generation: Mapped[int] = mapped_column(Integer)
    voice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    background: Mapped[str] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[float | None] = mapped_column(nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=1920)
    height: Mapped[int] = mapped_column(Integer, default=1080)
    bytes: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SocialAccount(Base):
    __tablename__ = "social_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    platform: Mapped[str] = mapped_column(String(24), unique=True)
    provider_account_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Publish(Base):
    __tablename__ = "publishes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    platform: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    privacy: Mapped[str] = mapped_column(String(64), default="private")
    caption: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    category_id: Mapped[str] = mapped_column(String(32), default="22")
    schedule_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    video_generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("chapter_id", "revision_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(100), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Step(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("job_id", "key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    output: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
