"""TikTok Direct Post FILE_UPLOAD; consent/UI and persistence belong to callers."""

import math
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .base import PublishClient, PublishError, file_body, upload_url, video_size

API = "https://open.tiktokapis.com/v2/post/publish"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CHUNK_SIZE = 10_000_000


def tiktok_authorize_url(client_key: str, redirect_uri: str, state: str, *, scope: str = "") -> str:
    query = urlencode({
        "client_key": client_key, "response_type": "code",
        "scope": scope or "video.publish,user.info.basic",
        "redirect_uri": redirect_uri, "state": state,
    })
    return f"{AUTH_URL}?{query}"


async def exchange_tiktok_code(code: str, client_key: str, client_secret: str,
                               redirect_uri: str, *, token_url: str = TOKEN_URL,
                               client: httpx.AsyncClient | None = None) -> dict:
    if not code.strip() or not redirect_uri.strip():
        raise PublishError("Thiếu thông tin OAuth của TikTok.")
    try:
        async with client or httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
            response = await http.post(token_url, data={
                "client_key": client_key, "client_secret": client_secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri,
            })
    except httpx.HTTPError:
        raise PublishError("Không kết nối được TikTok khi đăng nối tài khoản.") from None
    if response.status_code != 200:
        raise PublishError("TikTok từ chối mã đăng nối; kiểm tra ứng dụng và quyền.")
    data = response.json()
    if not isinstance(data, dict) or "access_token" not in data:
        raise PublishError("TikTok trả token không hợp lệ.")
    return data


class TikTokClient(PublishClient):
    platform = "TikTok"

    async def _post(self, endpoint: str, payload: dict) -> dict:
        response = await self.request("POST", API + endpoint, json=payload)
        body = self.json(response)
        error = body.get("error")
        if not isinstance(error, dict) or error.get("code") != "ok":
            raise PublishError("TikTok từ chối yêu cầu; kiểm tra quyền, hạn mức và trạng thái bài đăng.")
        if not isinstance(body.get("data"), dict):
            raise PublishError("TikTok trả dữ liệu không hợp lệ.")
        return body["data"]

    async def creator_info(self) -> dict:
        """Render current account/privacy options in the consent UI."""
        return await self._post("/creator_info/query/", {})

    async def user_info(self) -> dict:
        """Current account identity for storing display name after connect."""
        response = await self.request(
            "GET", "https://open.tiktokapis.com/v2/user/info/?fields=open_id,union_id,display_name,avatar_url_100")
        body = self.json(response)
        error = body.get("error")
        if not isinstance(error, dict) or error.get("code") != "ok":
            raise PublishError("TikTok từ chối lấy thông tin tài khoản.")
        user = (body.get("data") or {}).get("user") or {}
        return {"open_id": user.get("open_id"),
                "display_name": user.get("display_name") or "TikTok account"}

    async def initialize(
        self, path: str | Path, *, title: str, privacy_level: str,
        duration_seconds: float, consent: bool, disable_comment: bool = True,
        disable_duet: bool = True, disable_stitch: bool = True,
        is_aigc: bool = True, brand_content: bool = False, brand_organic: bool = False,
    ) -> dict:
        """Return publish_id/upload_url/source_info for durable storage BEFORE upload.

        No default privacy choice: callers must pass the user's explicit selection.
        Do not automatically retry init: its outcome may be ambiguous after a timeout.
        """
        size = video_size(Path(path), 4_000_000_000)
        if consent is not True:
            raise PublishError("Cần người dùng đồng ý trước khi đăng video.")
        if len(title.encode("utf-16-le")) // 2 > 2200:
            raise PublishError("Tiêu đề TikTok vượt giới hạn 2200 ký tự UTF-16.")
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise PublishError("Thời lượng video không hợp lệ.")
        creator = await self.creator_info()
        options = creator.get("privacy_level_options")
        maximum = creator.get("max_video_post_duration_sec")
        if not isinstance(options, list) or not isinstance(maximum, (int, float)):
            raise PublishError("TikTok trả thông tin quyền đăng không hợp lệ.")
        if privacy_level not in options:
            raise PublishError("Quyền riêng tư không nằm trong lựa chọn của tài khoản TikTok.")
        if duration_seconds > maximum:
            raise PublishError("Video vượt thời lượng tài khoản TikTok cho phép.")
        if brand_content and privacy_level == "SELF_ONLY":
            raise PublishError("Nội dung tài trợ không thể đặt riêng tư trên TikTok.")
        chunk = min(size, CHUNK_SIZE)
        source = {"source": "FILE_UPLOAD", "video_size": size,
                  "chunk_size": chunk, "total_chunk_count": max(1, size // chunk)}
        post = {"title": title, "privacy_level": privacy_level, "is_aigc": is_aigc,
                "brand_content_toggle": brand_content, "brand_organic_toggle": brand_organic}
        for key, selected in (("comment", disable_comment), ("duet", disable_duet),
                              ("stitch", disable_stitch)):
            if not isinstance(creator.get(key + "_disabled"), bool):
                raise PublishError("TikTok trả thông tin tương tác không hợp lệ.")
            post["disable_" + key] = selected or creator[key + "_disabled"]
        result = await self._post("/video/init/", {"post_info": post, "source_info": source})
        if not isinstance(result.get("publish_id"), str) or not result["publish_id"]:
            raise PublishError("TikTok không trả mã theo dõi bài đăng.")
        url = upload_url(result.get("upload_url", ""), "tiktokapis.com")
        return {"publish_id": result["publish_id"], "upload_url": url, "source_info": source}

    async def upload(self, path: str | Path, session: dict) -> None:
        """Only transfers bytes; successful upload is NOT publish completion."""
        path = Path(path)
        size = video_size(path, 4_000_000_000)
        url = upload_url(session.get("upload_url", ""), "tiktokapis.com")
        chunk = min(size, CHUNK_SIZE)
        count = max(1, size // chunk)
        expected = {"source": "FILE_UPLOAD", "video_size": size,
                    "chunk_size": chunk, "total_chunk_count": count}
        if session.get("source_info") != expected:
            raise PublishError("File video không khớp phiên tải TikTok.")
        for index in range(count):
            offset = index * chunk
            length = size - offset if index == count - 1 else chunk
            await self.request("PUT", url, authenticated=False,
                               expected=(201,) if index == count - 1 else (206,),
                               headers={"Content-Type": "video/mp4", "Content-Length": str(length),
                                        "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}"},
                               content=file_body(path, offset, length))

    async def status(self, publish_id: str) -> dict:
        if not publish_id.strip():
            raise PublishError("Thiếu mã theo dõi bài đăng TikTok.")
        # Preserve provider spelling: publicaly_available_post_id.
        result = await self._post("/status/fetch/", {"publish_id": publish_id})
        if result.get("status") not in {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD",
                                        "SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"}:
            raise PublishError("TikTok trả trạng thái bài đăng không hợp lệ.")
        return result
