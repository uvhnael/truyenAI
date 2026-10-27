"""Publish clients with mocked HTTP: TikTok Direct Post + YouTube resumable.

No live provider calls. base primitives (upload_url, video_size) are exercised
with real files on tmp_path.
"""

import json

import httpx
import pytest

from app.publish.base import PublishError, upload_url, video_size
from app.publish.tiktok import TikTokClient, exchange_tiktok_code, tiktok_authorize_url
from app.publish.youtube import (
    YoutubeClient,
    exchange_google_code,
    google_authorize_url,
    refresh_google_token,
)


class FakeTransport(httpx.AsyncBaseTransport):
    """Pluggable httpx.AsyncClient transport: call .add(handler) to queue responses."""

    def __init__(self):
        super().__init__()
        self.handlers = []

    def add(self, status=200, json_body=None, headers=None):
        self.handlers.append((status, json_body, headers or {}))

    async def handle_async_request(self, request):
        status, body, headers = self.handlers.pop(0) if self.handlers else (500, None, {})
        content = json.dumps(body).encode() if body is not None else b""
        return httpx.Response(status, content=content, headers=headers,
                              request=request)


@pytest.fixture
def proto_client():
    transport = FakeTransport()
    client = httpx.AsyncClient(transport=transport, timeout=10)
    return client, transport


@pytest.fixture
def sample_video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x2a\x00" * 1000)
    return path


# ---- base ----

def test_upload_url_accepts_expected_domain():
    assert upload_url("https://open-api.tiktokapis.com/v1/upload", "tiktokapis.com")


@pytest.mark.parametrize("url", [
    "http://tiktokapis.com/upload",
    "https://evil.com/upload",
    "https://open-api.tiktokapis.com:8443/upload",
    "https://user:pass@open-api.tiktokapis.com/upload",
    "not a url",
    "",
])
def test_upload_url_rejects_invalid(url):
    with pytest.raises(PublishError):
        upload_url(url, "tiktokapis.com")


def test_video_size_ok(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"\x00" * 500)
    assert video_size(path, 1000) == 500


def test_video_size_rejects_empty_or_oversize(tmp_path):
    empty = tmp_path / "e.mp4"
    empty.write_bytes(b"")
    with pytest.raises(PublishError):
        video_size(empty, 1000)
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00" * 2000)
    with pytest.raises(PublishError):
        video_size(big, 1000)


# ---- TikTok OAuth ----

def test_tiktok_authorize_url_contains_expected_params():
    url = tiktok_authorize_url("ck", "http://localhost:18080/x", "state123")
    assert "client_key=ck" in url and "response_type=code" in url
    assert "redirect_uri" in url and "state=state123" in url
    assert "video.publish" in url


@pytest.mark.asyncio
async def test_exchange_tiktok_code_success(proto_client):
    client, transport = proto_client
    transport.add(status=200, json_body={"access_token": "tok", "open_id": "oid"})
    result = await exchange_tiktok_code("code", "ck", "sec", "http://red", client=client)
    assert result["access_token"] == "tok"


@pytest.mark.asyncio
async def test_exchange_tiktok_code_rejects_bad_status(proto_client):
    client, transport = proto_client
    transport.add(status=400, json_body={"error": "bad"})
    with pytest.raises(PublishError):
        await exchange_tiktok_code("code", "ck", "sec", "http://red", client=client)


# ---- TikTok Direct Post ----

@pytest.mark.asyncio
async def test_tiktok_init_upload_full_flow(proto_client, sample_video):
    client, transport = proto_client
    # creator_info (privacy options + duration)
    transport.add(status=200, json_body={
        "error": {"code": "ok"},
        "data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                 "max_video_post_duration_sec": 300,
                 "comment_disabled": False, "duet_disabled": False, "stitch_disabled": False},
    })
    # video/init
    transport.add(status=200, json_body={
        "error": {"code": "ok"},
        "data": {"publish_id": "pid-1", "upload_url": "https://upload-aa.tiktokapis.com/v1/upload"},
    })
    # upload PUT (one chunk)
    transport.add(status=201, json_body={})

    async with TikTokClient("tok", client=client) as tiktok:
        created = await tiktok.initialize(
            sample_video, title="Miss", privacy_level="PUBLIC_TO_EVERYONE",
            duration_seconds=10, consent=True)
        assert created["publish_id"] == "pid-1"
        assert created["upload_url"].startswith("https://")
        await tiktok.upload(sample_video, {"upload_url": created["upload_url"],
                                           "source_info": created["source_info"]})
    assert len(transport.handlers) == 0  # every queued response consumed


@pytest.mark.asyncio
async def test_tiktok_init_requires_consent(proto_client, sample_video):
    client, transport = proto_client
    with pytest.raises(PublishError):
        async with TikTokClient("tok", client=client) as tiktok:
            await tiktok.initialize(sample_video, title="No", privacy_level="SELF_ONLY",
                                    duration_seconds=1, consent=False)


@pytest.mark.asyncio
async def test_tiktok_upload_rejects_mismatched_session(proto_client, sample_video):
    client, transport = proto_client
    # creator response still consumed before the mismatch is detected? No: initialize is not called.
    async with TikTokClient("tok", client=client) as tiktok:
        with pytest.raises(PublishError):
            await tiktok.upload(sample_video, {"upload_url": "https://x.tiktokapis.com/u",
                                               "source_info": {"video_size": 999, "chunk_size": 1,
                                                               "total_chunk_count": 1}})


# ---- YouTube OAuth ----

def test_google_authorize_url_shape():
    url = google_authorize_url("cid", "http://red", "st")
    assert "client_id=cid" in url and "redirect_uri" in url and "state=st" in url
    assert "umli" not in url  # no junk


@pytest.mark.asyncio
async def test_exchange_google_code_success(proto_client):
    client, transport = proto_client
    transport.add(status=200, json_body={
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "youtube.upload"})
    result = await exchange_google_code("code", "cid", "sec", "http://red", client=client)
    assert result["access_token"] == "at" and result["refresh_token"] == "rt"


@pytest.mark.asyncio
async def test_refresh_google_token_keeps_refresh(proto_client):
    client, transport = proto_client
    transport.add(status=200, json_body={"access_token": "new-at", "expires_in": 3600})
    result = await refresh_google_token("rt", "cid", "sec", client=client)
    assert result["access_token"] == "new-at"
    assert result["refresh_token"] == "rt"  # preserved when provider omits it


# ---- YouTube resumable upload ----

@pytest.mark.asyncio
async def test_youtube_init_upload_status(proto_client, sample_video):
    client, transport = proto_client
    # init resumable session -> location header
    transport.add(status=200, json_body={},
                  headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=abc"})
    # upload PUT (final chunk) -> video id
    transport.add(status=200, json_body={"id": "vid-9", "kind": "youtube#video"})
    # status videos.list
    transport.add(status=200, json_body={
        "items": [{"id": "vid-9", "snippet": {"title": "t"},
                   "status": {"privacyStatus": "public", "processingDetails": {"processingStatus": "succeeded"}}}]})

    async with YoutubeClient("tok", client=client) as yt:
        session = await yt.initialize(sample_video, title="Clip", category_id="22", privacy="public")
        assert session["upload_url"].startswith("https://")
        final = await yt.upload(sample_video, session)
        assert final["id"] == "vid-9"
        status = await yt.status("vid-9")
        assert status["status"] == "published"
        assert status["remote_url"].endswith("watch?v=vid-9")
    assert len(transport.handlers) == 0


@pytest.mark.asyncio
async def test_youtube_init_rejects_bad_privacy(proto_client, sample_video):
    client, transport = proto_client
    async with YoutubeClient("tok", client=client) as yt:
        with pytest.raises(PublishError):
            await yt.initialize(sample_video, title="x", privacy="secret")


@pytest.mark.asyncio
async def test_channel_info_requires_items(proto_client):
    client, transport = proto_client
    transport.add(status=200, json_body={"items": []})
    async with YoutubeClient("tok", client=client) as yt:
        with pytest.raises(PublishError):
            await yt.channel_info()