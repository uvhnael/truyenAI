"""YouTube Data API v3 resumable upload; OAuth consent/persistence belong to callers.

Only transfers bytes and metadata — does not create DB rows, schedule or decide privacy.
"""

from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from .base import PublishClient, PublishError, file_body, video_size

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
DATA_URL = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload " \
         "https://www.googleapis.com/auth/youtube.readonly"
CHUNK_SIZE = 10_000_000


def google_authorize_url(client_id: str, redirect_uri: str, state: str, *, extra_scopes: str = "") -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": (SCOPES + " " + extra_scopes).strip(),
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


async def exchange_google_code(code: str, client_id: str, client_secret: str,
                               redirect_uri: str, *, token_url: str = TOKEN_URL,
                               client: httpx.AsyncClient | None = None) -> dict:
    if not redirect_uri.strip() or not code.strip():
        raise PublishError("Thiếu thông tin OAuth của YouTube.")
    try:
        async with client or httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
            response = await http.post(token_url, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            })
    except httpx.HTTPError:
        raise PublishError("Không kết nối được Google khi đăng nối YouTube.") from None
    if response.status_code != 200:
        raise PublishError("Google từ chối trao đổi mã đăng nối; kiểm tra ứng dụng OAuth.")
    data = response.json()
    if not isinstance(data, dict) or "access_token" not in data:
        raise PublishError("Google trả token không hợp lệ.")
    return data


async def refresh_google_token(refresh_token: str, client_id: str, client_secret: str,
                               *, token_url: str = TOKEN_URL,
                               client: httpx.AsyncClient | None = None) -> dict:
    if not refresh_token.strip():
        raise PublishError("Thiếu refresh token của YouTube; hãy kết nối lại tài khoản.")
    try:
        async with client or httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
            response = await http.post(token_url, data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            })
    except httpx.HTTPError:
        raise PublishError("Không kết nối được Google khi làm mới token.") from None
    if response.status_code != 200:
        raise PublishError("Google từ chối làm mới token; hãy kết nối lại tài khoản YouTube.")
    data = response.json()
    if not isinstance(data, dict) or "access_token" not in data:
        raise PublishError("Google trả token làm mới không hợp lệ.")
    if "refresh_token" not in data:
        data = {**data, "refresh_token": refresh_token}
    return data


def _safe_scope(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname is not None
                 and parts.hostname.endswith("googleapis.com") and not parts.username
                 and not parts.password and not parts.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PublishError("Phiên tải YouTube trả địa chỉ tải không hợp lệ.")
    return url


class YoutubeClient(PublishClient):
    platform = "YouTube"

    async def _json(self, url, *, params=None) -> dict:
        response = await self.request("GET", url, params=params or {})
        body = self.json(response)
        if body.get("error"):
            raise PublishError("YouTube từ chối yêu cầu; kiểm tra quyền và hạn mức.")
        return body

    async def channel_info(self) -> dict:
        body = await self._json(f"{DATA_URL}/channels", params={
            "part": "snippet", "mine": "true"})
        items = body.get("items") or []
        if not items:
            raise PublishError("Không tìm thấy kênh YouTube của tài khoản này.")
        snippet = items[0].get("snippet") or {}
        return {"id": items[0].get("id"), "title": snippet.get("title"),
                "thumbnails": snippet.get("thumbnails") or {}}

    async def initialize(self, path: str | Path, *, title: str, description: str = "",
                         tags: list | None = None, category_id: str = "22",
                         privacy: str = "private", schedule_at: str | None = None,
                         made_for_kids: bool = False) -> dict:
        """Return the resumable upload_url for durable storage BEFORE transfer."""
        if not isinstance(privacy, str) or privacy not in ("private", "unlisted", "public"):
            raise PublishError("Quyền riêng tư YouTube không hợp lệ.")
        if not title.strip():
            raise PublishError("Thiếu tiêu đề video YouTube.")
        video_size(Path(path), 50_000_000_000)  # validate existence/length only
        snippet = {"title": title, "description": description,
                   "tags": [t.strip() for t in (tags or []) if str(t).strip()][:500],
                   "categoryId": str(category_id)}
        status = {"privacyStatus": privacy.upper(), "selfDeclaredMadeForKids": bool(made_for_kids)}
        if schedule_at:
            status["publishAt"] = schedule_at
        payload = {"snippet": snippet, "status": status}
        size = Path(path).stat().st_size
        chunk = min(size, CHUNK_SIZE)
        response = await self.request(
            "POST", f"{UPLOAD_URL}?uploadType=resumable", json=payload,
            headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
        location = response.headers.get("location")
        if not location:
            raise PublishError("YouTube không trả phiên tải resumable.")
        return {"upload_url": _safe_scope(location),
                "source_info": {"video_size": size, "chunk_size": chunk,
                                "total_chunk_count": max(1, size // chunk)}}

    async def upload(self, path: str | Path, session: dict) -> dict:
        """Transfer bytes; final response carries the remote video id."""
        path = Path(path)
        size = video_size(path, 50_000_000_000)
        url = _safe_scope(session.get("upload_url", ""))
        chunk = min(size, CHUNK_SIZE)
        count = max(1, size // chunk)
        expected = {"video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        if session.get("source_info") != expected:
            raise PublishError("File video không khớp phiên tải YouTube.")
        for index in range(count):
            offset = index * chunk
            length = size - offset if index == count - 1 else chunk
            final = index == count - 1
            response = await self.request(
                "PUT", url,
                headers={"Content-Type": "video/mp4",
                         "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}"},
                expected=(201, 200) if final else (308,),
                content=file_body(path, offset, length))
            if final:
                return self.json(response)
        return {}

    async def status(self, video_id: str) -> dict:
        if not str(video_id).strip():
            raise PublishError("Thiếu mã video YouTube.")
        body = await self._json(f"{DATA_URL}/videos", params={
            "part": "status,snippet", "id": video_id})
        items = body.get("items") or []
        if not items:
            raise PublishError("Không tìm thấy video YouTube đã tải.")
        status = items[0].get("status") or {}
        processing = status.get("processingDetails") or {}
        provider = status.get("privacyStatus", "").lower()
        provider_status = processing.get("processingStatus", "succeeded")
        if provider_status in ("processing", "failed"):
            mapped = "processing" if provider_status == "processing" else "failed"
        else:
            mapped = "published" if provider in ("public", "unlisted") else "processing"
        snippet = items[0].get("snippet") or {}
        return {"status": mapped, "remote_id": video_id,
                "remote_url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get("title")}