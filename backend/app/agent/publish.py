""""Publishing to TikTok/YouTube with durable state, token refresh and generation fencing.

Worker owns a global PG advisory execution lock; every write re-checks the fenced
generation so a retry/duplicate delivery cannot finalize a cancelled publish. No
consistent relationship exists between Publish and Video, so events record project_id
explicitly from a snapshot taken under the lock.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..db import Session
from ..models import Event, Publish, SocialAccount, Video

# Publish status the worker manages: queued -> uploading -> processing -> published / failed.


class PublishError(RuntimeError):
    """Safe Vietnamese message for a durable event or API response."""


async def _mark(s, project_id: str, publish_id: str, kind: str, **payload):
    s.add(Event(project_id=project_id, type=f"publish_{kind}",
                payload={"publish_id": publish_id, **payload}))


def _fresh_token(account: SocialAccount) -> str:
    if account.expires_at and account.expires_at.tzinfo is None:
        account.expires_at = account.expires_at.replace(tzinfo=UTC)
    # A token is stale only when it has a concrete expiry in the past. Platforms
    # like TikTok may not return an expiry; treat that as usable.
    expired = account.expires_at is not None and account.expires_at <= datetime.now(UTC)
    return "" if expired else account.access_token


async def run_publish(publish_id: str, generation: int):
    """Called by the worker inside a PG advisory lock; writes are generation-fenced."""
    async with Session() as s, s.begin():
        publish = await s.scalar(
            select(Publish).where(Publish.id == publish_id).with_for_update())
        if not publish or publish.generation != generation or publish.status in (
                "published", "failed"):
            return
        account = await s.scalar(select(SocialAccount).where(
            SocialAccount.platform == publish.platform).with_for_update())
        if not account:
            publish.status = "failed"
            publish.error = "Chưa kết nối tài khoản; hãy kết nối trước khi đăng."
            return
        video = await s.scalar(select(Video).where(Video.id == publish.video_id))
        if not video or video.status != "ready" or not video.path:
            publish.status = "failed"
            publish.error = "Video chưa sẵn sàng; hãy render video trước."
            return
        token = _fresh_token(account)
        if not token and account.platform == "youtube":
            try:
                from ..publish.youtube import refresh_google_token
                refreshed = await refresh_google_token(
                    account.refresh_token or "", settings.youtube_client_id,
                    settings.youtube_client_secret)
            except Exception as exc:
                publish.status = "failed"
                publish.error = f"Không làm mới token YouTube: {exc}"
                return
            token = refreshed["access_token"]
            account.access_token = token
            if refreshed.get("expires_in"):
                account.expires_at = datetime.now(UTC) + timedelta(seconds=int(refreshed["expires_in"]))
        if not token:
            publish.status = "failed"
            publish.error = "Token tài khoản hết hạn; hãy kết nối lại tài khoản."
            return
        publish.status = "uploading"
        await _mark(s, video.project_id, publish.id, "uploading")
        snap = {
            "publish": publish.id, "platform": publish.platform, "token": token,
            "video_path": video.path, "project_id": video.project_id,
            "title": publish.title or "", "description": publish.description or "",
            "tags": list(publish.tags or []), "caption": publish.caption or "",
            "privacy": publish.privacy, "category_id": publish.category_id,
            "schedule_at": publish.schedule_at, "generation": generation,
        }
    await _upload_body(snap)


async def _upload_body(snap: dict):
    platform = snap["platform"]
    try:
        if platform == "tiktok":
            result = await _publish_tiktok(snap)
        elif platform == "youtube":
            result = await _publish_youtube(snap)
        else:
            raise PublishError("Nền tảng đăng bài không được hỗ trợ.")
    except PublishError as exc:
        await _fail(snap, str(exc))
        return
    except Exception as exc:
        await _fail(snap, f"Đăng thất bại ({type(exc).__name__}); kiểm tra kết nối nền tảng.")
        return
    await _finalize(snap, result["remote_id"], result.get("processing", True))


async def _publish_tiktok(snap: dict) -> dict:
    from ..publish.tiktok import TikTokClient
    path = Path(snap["video_path"])
    privacy = (snap["privacy"] or "SELF_ONLY").upper()
    async with TikTokClient(snap["token"]) as client:
        created = await client.initialize(path, title=snap["caption"], privacy_level=privacy,
                                          duration_seconds=1, consent=True)
        await client.upload(path, {"upload_url": created["upload_url"],
                                   "source_info": created["source_info"]})
        return {"remote_id": created["publish_id"], "processing": True}


async def _publish_youtube(snap: dict) -> dict:
    from ..publish.youtube import YoutubeClient
    path = Path(snap["video_path"])
    schedule = snap["schedule_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if snap["schedule_at"] else None
    async with YoutubeClient(snap["token"]) as client:
        session = await client.initialize(
            path, title=snap["title"], description=snap["description"],
            tags=(snap["tags"] if snap["tags"] else []), category_id=snap["category_id"],
            privacy=snap["privacy"] or "private", schedule_at=schedule)
        final = await client.upload(path, session)
        video_id = final.get("id") or session.get("remote_id")
        if not video_id:
            raise PublishError("YouTube không trả mã video sau khi tải.")
        return {"remote_id": video_id, "processing": True,
                "remote_url": f"https://www.youtube.com/watch?v={video_id}"}


async def _finalize(snap: dict, remote_id: str, processing: bool):
    async with Session() as s, s.begin():
        publish = await s.scalar(
            select(Publish).where(Publish.id == snap["publish"]).with_for_update())
        if not publish or publish.generation != snap["generation"]:
            return
        publish.remote_id = remote_id
        publish.remote_url = snap.get("remote_url") or publish.remote_url
        publish.status = "processing" if processing else "published"
        await _mark(s, snap["project_id"], publish.id,
                    "published" if not processing else "processing", remote_id=remote_id)


async def _fail(snap: dict, detail: str):
    async with Session() as s, s.begin():
        publish = await s.scalar(
            select(Publish).where(Publish.id == snap["publish"]).with_for_update())
        if not publish or publish.generation != snap["generation"]:
            return
        publish.status, publish.error = "failed", detail
        await _mark(s, snap["project_id"], publish.id, "failed", error=detail)