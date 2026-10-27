"""Small async publishing primitives; never log tokens or provider bodies."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class PublishError(RuntimeError):
    """Safe Vietnamese message suitable for an API response or durable event."""


def upload_url(value: str, domain: str) -> str:
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname is not None
                 and (url.hostname == domain or url.hostname.endswith("." + domain))
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PublishError("Địa chỉ tải video do nền tảng trả về không hợp lệ.")
    return value


def video_size(path: Path, maximum: int) -> int:
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    if not 0 < size <= maximum:
        raise PublishError("File video không tồn tại, rỗng hoặc vượt giới hạn dung lượng.")
    return size


async def file_body(path: Path, offset: int, length: int):
    """Bounded reads, including for a multi-GB single-request upload."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            while length:
                block = await asyncio.to_thread(stream.read, min(length, 1024 * 1024))
                if not block:
                    raise PublishError("File video đã thay đổi trong lúc tải lên.")
                length -= len(block)
                yield block
    except OSError:
        raise PublishError("Không đọc được file video.") from None


class PublishClient:
    platform = "Nền tảng"

    def __init__(self, access_token: str, *, client: httpx.AsyncClient | None = None):
        if not access_token.strip() or any(c in access_token for c in "\r\n"):
            raise PublishError("Thiếu hoặc sai access token; hãy kết nối lại tài khoản.")
        self._token = access_token
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self._owned:
            await self._client.aclose()

    async def request(self, method, url, *, authenticated=True, expected=(200,), **kwargs):
        headers = kwargs.pop("headers", {})
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        # Build without inheriting client-level credentials/cookies on signed uploads.
        request = httpx.Request(method, url, headers=headers, **kwargs)
        request.extensions["timeout"] = httpx.Timeout(120).as_dict()
        try:
            response = await self._client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError:
            raise PublishError(f"Không kết nối được {self.platform}; kiểm tra trạng thái trước khi thử lại.") from None
        if response.status_code not in expected:
            if response.status_code in (401, 403):
                detail = "Tài khoản chưa được cấp quyền, token hết hạn hoặc ứng dụng chưa được duyệt."
            elif response.status_code == 429:
                detail = "Đã vượt giới hạn yêu cầu; thử lại sau."
            else:
                detail = f"Yêu cầu thất bại (HTTP {response.status_code}); kiểm tra trạng thái trước khi thử lại."
            raise PublishError(f"{self.platform}: {detail}")
        return response

    def json(self, response):
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise PublishError(f"{self.platform} trả dữ liệu không hợp lệ.") from None
        return data
