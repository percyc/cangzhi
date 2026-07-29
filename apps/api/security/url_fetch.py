from __future__ import annotations

import http.client
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .url_safety import URLSecurityError, normalize_url, resolve_allowed_addresses

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/116.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.47.2560(0x28002F37) WeChat/arm64 "
    "Weixin NetType/WIFI Language/zh_CN ABI/arm64"
)
DEFAULT_USER_AGENT = DESKTOP_USER_AGENT
_CHUNK_SIZE = 64 * 1024
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}

_DESKTOP_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-CH-UA": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_MOBILE_BROWSER_HEADERS = {
    **_DESKTOP_BROWSER_HEADERS,
    "Sec-CH-UA-Mobile": "?1",
    "Sec-CH-UA-Platform": '"Android"',
}


@dataclass(frozen=True)
class BrowserProfile:
    name: str
    user_agent: str
    headers: Mapping[str, str]


def browser_profiles_for_url(url: str) -> tuple[BrowserProfile, ...]:
    """Return realistic browser profiles in the order they should be tried."""

    hostname = (urlsplit(url).hostname or "").lower()
    desktop = BrowserProfile(
        "desktop_chrome", DESKTOP_USER_AGENT, _DESKTOP_BROWSER_HEADERS
    )
    mobile = BrowserProfile("mobile_chrome", MOBILE_USER_AGENT, _MOBILE_BROWSER_HEADERS)
    if hostname == "mp.weixin.qq.com" or hostname.endswith(".weixin.qq.com"):
        return (
            BrowserProfile(
                "wechat_android",
                WECHAT_USER_AGENT,
                {**_MOBILE_BROWSER_HEADERS, "Referer": "https://mp.weixin.qq.com/"},
            ),
            desktop,
        )
    return (desktop, mobile)


def looks_like_access_block(raw_bytes: bytes) -> bool:
    """Detect common challenge pages so they are never indexed as articles."""

    preview = raw_bytes[:256_000].decode("utf-8", errors="ignore").lower()
    markers = (
        "当前环境异常",
        "完成验证后即可继续访问",
        "访问过于频繁",
        "请输入验证码",
        "安全验证",
        "verify you are human",
        "checking your browser",
        "cf-chl-",
        "captcha",
    )
    return any(marker.lower() in preview for marker in markers)


class URLFetchError(RuntimeError):
    """Raised when a remote page cannot be fetched safely."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str
    raw_bytes: bytes


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a checked IP while validating the URL host."""

    def __init__(
        self,
        connect_host: str,
        port: int,
        *,
        server_hostname: str,
        timeout: float,
    ) -> None:
        super().__init__(
            connect_host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._server_hostname = server_hostname

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw_socket,
            server_hostname=self._server_hostname,
        )


def _content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _open_connection(
    scheme: str,
    address: str,
    port: int,
    hostname: str,
    timeout_seconds: float,
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(
            address,
            port,
            server_hostname=hostname,
            timeout=timeout_seconds,
        )
    return http.client.HTTPConnection(address, port=port, timeout=timeout_seconds)


def fetch_url(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float = 20.0,
    max_redirects: int = 5,
    user_agent: str = DEFAULT_USER_AGENT,
    follow_redirects: bool = True,
    accept: str = "text/html,application/xhtml+xml",
    allowed_content_types: set[str] | None = None,
    request_headers: Mapping[str, str] | None = None,
) -> FetchedPage:
    """Fetch a page with DNS/IP validation on every redirect.

    The defaults only accept HTML. Trusted internal adapters may opt into
    additional response types and add protocol-specific headers.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    original_url = url
    current = normalize_url(url)
    visited: set[str] = set()

    for redirect_count in range(max_redirects + 1):
        if current in visited:
            raise URLFetchError("检测到重定向循环")
        visited.add(current)

        parts = urlsplit(current)
        hostname = parts.hostname or ""
        # Every DNS answer must be public. Connect to one of the exact checked
        # addresses so a second resolver lookup cannot rebind to an internal IP.
        allowed = resolve_allowed_addresses(hostname)
        address = next(
            (item.address for item in allowed if item.family == socket.AF_INET),
            allowed[0].address,
        )
        port = parts.port or (443 if parts.scheme == "https" else 80)
        request_target = parts.path or "/"
        if parts.query:
            request_target += f"?{parts.query}"
        default_port = 443 if parts.scheme == "https" else 80
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        host_header = (
            rendered_host if port == default_port else f"{rendered_host}:{port}"
        )
        connection = _open_connection(
            parts.scheme,
            address,
            port,
            hostname,
            timeout_seconds,
        )
        try:
            headers = {
                **_DESKTOP_BROWSER_HEADERS,
                "Host": host_header,
                "User-Agent": user_agent,
                "Accept": accept,
                "Connection": "close",
            }
            if request_headers:
                protected = {"host", "connection", "content-length"}
                for key, value in request_headers.items():
                    if key.lower() not in protected:
                        headers[key] = value
            connection.request(
                "GET",
                request_target,
                headers=headers,
            )
            response = connection.getresponse()

            if 300 <= response.status < 400 and follow_redirects:
                location = response.getheader("Location")
                if not location:
                    raise URLFetchError("重定向响应缺少目标地址")
                if redirect_count >= max_redirects:
                    raise URLFetchError("重定向次数过多")
                current = normalize_url(urljoin(current, location))
                continue

            if response.status < 200 or response.status >= 300:
                raise URLFetchError(f"远端返回状态码 {response.status}")

            media_type = _content_type(response.getheader("Content-Type"))
            accepted_types = allowed_content_types or _HTML_CONTENT_TYPES
            if media_type not in accepted_types:
                raise URLFetchError("远端返回了不支持的内容类型")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise URLFetchError("响应内容超过大小限制")
                except ValueError:
                    raise URLFetchError("远端返回了无效的内容长度") from None

            chunks: list[bytes] = []
            total = 0
            while chunk := response.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    raise URLFetchError("响应内容超过大小限制")
                chunks.append(chunk)
            body = b"".join(chunks)
            if not body:
                raise URLFetchError("响应内容为空")
            return FetchedPage(
                url=original_url,
                final_url=current,
                status_code=response.status,
                content_type=response.getheader("Content-Type") or media_type,
                raw_bytes=body,
            )
        except (URLFetchError, URLSecurityError):
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise URLFetchError(f"抓取失败：{exc}") from None
        finally:
            connection.close()

    raise URLFetchError("重定向次数过多")
