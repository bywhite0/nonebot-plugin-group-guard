"""极验后端协议与同源链接校验。"""

from urllib.parse import urlsplit

import httpx


class BackendError(RuntimeError):
    """后端不可用或协议异常，不代表成员答错。"""


def _origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise BackendError("验证服务地址无效")
    if "\\" in url or "#" in url:
        raise BackendError("验证服务地址无效")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if parts.scheme not in ("http", "https") or not host or "%" in host:
            raise ValueError
        if "@" in parts.netloc:
            raise ValueError
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii").lower()
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise BackendError("验证服务地址无效") from None
    return parts.scheme, host, port if port is not None else (443 if parts.scheme == "https" else 80)


class VerificationBackend:
    def __init__(self, base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None):
        self._origin = _origin(base_url)
        if "?" in base_url:
            raise BackendError("验证服务基地址不能包含查询参数")
        if not isinstance(api_key, str) or not api_key.strip() or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise BackendError("验证服务密钥未配置或无效")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = httpx.Timeout(15.0, connect=5.0)
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False,
        )

    async def _post(self, path: str, payload: dict, *, checking: bool = False) -> dict:
        try:
            response = await self._client.post(
                self._base_url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                auth=None,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except (httpx.HTTPError, httpx.InvalidURL, RuntimeError, ValueError):
            # 不携带原异常链，避免上游错误正文或请求信息泄露凭据。
            raise BackendError("验证服务请求失败，请稍后重试") from None
        if response.status_code != 200 and not (checking and response.status_code == 400):
            raise BackendError("验证服务 HTTP 响应异常")
        try:
            result = response.json()
        except (ValueError, UnicodeError, RecursionError):
            raise BackendError("验证服务响应不是有效 JSON") from None
        invalid_codes = {
            "验证失败：验证码已使用", "验证失败：验证码已过期",
            "验证失败：验证码未完成验证", "验证失败：验证码不存在或已失效",
            "验证失败：用户ID不匹配",
        }
        if (checking and isinstance(result, dict) and response.status_code in (200, 400)
                and type(result.get("code")) is int and result["code"] == 400
                and result.get("passed") is False and result.get("msg") in invalid_codes):
            return {"code": 0, "passed": False}
        if response.status_code != 200:
            raise BackendError("验证服务 HTTP 响应异常")
        if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0:
            raise BackendError("验证服务返回错误或无效状态")
        return result

    async def create(self, gid, uid) -> str:
        result = await self._post("/api/verify/create", {"group_id": str(gid), "user_id": str(uid)})
        data = result.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if (
            not isinstance(url, str) or not url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url or "#" in url
        ):
            raise BackendError("验证服务未返回有效链接")
        # 兼容站内绝对路径，不接受协议相对地址，保留完整路径与查询串。
        if url.startswith("/") and not url.startswith("//"):
            base = urlsplit(self._base_url)
            url = f"{base.scheme}://{base.netloc}{url}"
        if _origin(url) != self._origin:
            raise BackendError("验证链接不属于配置的服务来源")
        return url

    async def check(self, gid, uid, code) -> bool:
        result = await self._post(
            "/api/verify/check",
            {"group_id": str(gid), "user_id": str(uid), "code": code},
            checking=True,
        )
        passed = result.get("passed")
        if type(passed) is not bool:
            raise BackendError("验证服务未返回有效验证结果")
        return passed

    async def close(self):
        if self._owns_client:
            await self._client.aclose()
