from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .url_safety import URLSecurityError, normalize_url, resolve_allowed_addresses

DEFAULT_USER_AGENT = "Cangzhi/0.2 (+personal knowledge base)"
_CHUNK_SIZE = 64 * 1024
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


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
) -> FetchedPage:
    """Fetch an HTML page with DNS/IP validation on every redirect."""
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
            connection.request(
                "GET",
                request_target,
                headers={
                    "Host": host_header,
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                    "Connection": "close",
                },
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
            if media_type not in _HTML_CONTENT_TYPES:
                raise URLFetchError("仅支持 HTML 页面")
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
