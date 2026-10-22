from __future__ import annotations

import asyncio
import base64
import hashlib
import posixpath
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from ..security.url_safety import URLSecurityError, normalize_url


class WebDAVError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    is_collection: bool
    etag: str | None
    last_modified: str | None
    size: int | None
    content_type: str | None


def normalized_remote_path(path: str) -> str:
    # Entries are decoded once by parse_multistatus; do not decode again.
    return "/" + posixpath.normpath("/" + path.lstrip("/")).lstrip("/")


def scan_root_path(base_url: str, root_path: str) -> str:
    url = urljoin(base_url.rstrip("/") + "/", (root_path or "/").lstrip("/"))
    return normalized_remote_path(unquote(urlsplit(url).path))


def matches_ignore_pattern(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatchcase(path, pattern)
        or any(fnmatchcase(part, pattern) for part in path.split("/") if part)
        for pattern in patterns
    )


def in_scan_scope(path: str, root: str, recursive: bool, patterns: list[str]) -> bool:
    path = normalized_remote_path(path)
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix) or path == root:
        return False
    return (recursive or "/" not in path[len(prefix):]) and not matches_ignore_pattern(
        path, patterns
    )


async def scan_tree(
    *,
    base_url: str,
    root_path: str,
    username: str,
    password: str,
    trusted_private_network: bool,
    recursive: bool,
    ignore_patterns: list[str],
    list_directory: Callable[..., Awaitable[list[RemoteEntry]]] | None = None,
    max_directories: int = 2000,
    max_entries: int = 100000,
    timeout_seconds: float = 45,
) -> tuple[list[RemoteEntry], int, str]:
    """Return a complete bounded listing or fail without any reconciliation."""
    list_directory = list_directory or propfind
    root = scan_root_path(base_url, root_path)
    pending = deque([root])
    visited = {root}
    files: dict[str, RemoteEntry] = {}
    scanned = count = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            while pending:
                directory = pending.popleft()
                entries = await list_directory(
                    base_url=base_url, root_path=directory, username=username,
                    password=password, trusted_private_network=trusted_private_network,
                    absolute_path=True, depth="1",
                )
                scanned += 1
                count += len(entries)
                if count > max_entries:
                    raise WebDAVError("扫描未完成：目录条目过多，请缩小同步目录；原记录保持不变")
                for entry in entries:
                    path = normalized_remote_path(entry.path)
                    if path == directory:
                        continue
                    # Never follow siblings, ancestors, or unexpected deep responses.
                    if posixpath.dirname(path) != directory:
                        raise WebDAVError("扫描未完成：目录返回越界条目；原记录保持不变")
                    if matches_ignore_pattern(path, ignore_patterns):
                        continue
                    if entry.is_collection:
                        if recursive and path not in visited:
                            visited.add(path)
                            if len(visited) > max_directories:
                                raise WebDAVError("扫描未完成：子目录过多，请缩小同步目录；原记录保持不变")
                            pending.append(path)
                    else:
                        files[path] = RemoteEntry(path, False, entry.etag,
                            entry.last_modified, entry.size, entry.content_type)
    except TimeoutError as exc:
        raise WebDAVError("扫描未完成：扫描超时，请缩小同步目录后重试；原记录保持不变") from exc
    return list(files.values()), scanned, root


def webdav_external_identity(base_url: str, remote_path: str) -> str:
    """Return a credential- and connector-independent identity for a remote file."""

    parts = urlsplit((base_url or "").strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    normalized_path = "/" + posixpath.normpath(
        "/" + unquote(remote_path or "").lstrip("/")
    ).lstrip("/")
    canonical = f"webdav:{scheme}://{authority}{normalized_path}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_webdav_url(url: str, *, trusted_private_network: bool) -> str:
    candidate = (url or "").strip().rstrip("/") + "/"
    if not trusted_private_network:
        return normalize_url(candidate)
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise URLSecurityError("WebDAV 地址必须是有效的 HTTP/HTTPS 地址")
    if parts.username or parts.password:
        raise URLSecurityError("WebDAV 地址不能包含用户名或密码")
    return candidate


async def propfind(
    *,
    base_url: str,
    root_path: str,
    username: str,
    password: str,
    trusted_private_network: bool,
    depth: str = "1",
    timeout_seconds: float = 20,
    absolute_path: bool = False,
    client: httpx.AsyncClient | None = None,
) -> list[RemoteEntry]:
    safe_base = validate_webdav_url(
        base_url, trusted_private_network=trusted_private_network
    )
    path = "/" + (root_path or "/").strip("/")
    if absolute_path:
        parts = urlsplit(safe_base)
        request_url = f"{parts.scheme}://{parts.netloc}{quote(path, safe='/').rstrip('/')}/"
    else:
        request_url = urljoin(safe_base, path.lstrip("/"))
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        # Reused clients must not carry a server session between connectors.
        "Cookie": "",
        "Depth": depth,
        "Content-Type": "application/xml; charset=utf-8",
        "Accept": "application/xml,text/xml",
    }
    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:resourcetype/><d:getetag/><d:getlastmodified/>
<d:getcontentlength/><d:getcontenttype/>
</d:prop></d:propfind>"""
    try:
        async with (nullcontext(client) if client else httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )) as client:
            async with client.stream(
                "PROPFIND", request_url, headers=headers, content=body
            ) as response:
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 16 * 1024 * 1024:
                        raise WebDAVError("扫描未完成：单目录响应超过16 MiB，请缩小目录；原记录保持不变")
    except httpx.HTTPError as exc:
        raise WebDAVError(f"无法连接 WebDAV：{type(exc).__name__}") from exc
    if response.status_code not in {200, 207}:
        if response.status_code in {401, 403}:
            raise WebDAVError("WebDAV 认证失败，请检查用户名和密码")
        raise WebDAVError(f"WebDAV 返回 HTTP {response.status_code}")
    return parse_multistatus(bytes(payload))


async def download_file(
    *,
    base_url: str,
    remote_path: str,
    username: str,
    password: str,
    trusted_private_network: bool,
    max_bytes: int,
    timeout_seconds: float = 60,
) -> tuple[bytes, str]:
    safe_base = validate_webdav_url(
        base_url, trusted_private_network=trusted_private_network
    )
    parts = urlsplit(safe_base)
    request_url = f"{parts.scheme}://{parts.netloc}{remote_path}"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = None
            send_authorization = True
            for _redirect in range(4):
                request_headers = (
                    {"Authorization": f"Basic {token}"}
                    if send_authorization
                    else {}
                )
                response = await client.get(
                    request_url,
                    headers=request_headers,
                )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location")
                if not location:
                    raise WebDAVError("WebDAV 下载跳转缺少目标地址")
                target = urljoin(request_url, location)
                target_parts = urlsplit(target)
                same_origin = (
                    target_parts.scheme == parts.scheme
                    and target_parts.netloc == parts.netloc
                )
                if send_authorization and not same_origin:
                    # Object-storage redirects use a signed public URL.
                    # Validate it as an ordinary public URL and never
                    # forward the WebDAV Basic credential.
                    target = normalize_url(target)
                    send_authorization = False
                elif not send_authorization:
                    target = normalize_url(target)
                request_url = target
            assert response is not None
    except httpx.HTTPError as exc:
        raise WebDAVError(f"下载 WebDAV 文件失败：{type(exc).__name__}") from exc
    if response.status_code != 200:
        raise WebDAVError(f"下载 WebDAV 文件返回 HTTP {response.status_code}")
    if len(response.content) > max_bytes:
        raise WebDAVError(f"WebDAV 文件超过 {max_bytes // 1024 // 1024} MB 限制")
    if not response.content:
        raise WebDAVError("WebDAV 文件内容为空")
    return response.content, response.headers.get(
        "Content-Type", "application/octet-stream"
    )


def parse_multistatus(payload: bytes) -> list[RemoteEntry]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WebDAVError("WebDAV 返回的目录清单不是有效 XML") from exc
    entries: list[RemoteEntry] = []
    ns = {"d": "DAV:"}
    if root.tag != "{DAV:}multistatus":
        raise WebDAVError("扫描未完成：WebDAV 返回的目录清单缺少 multistatus")
    for response in root.findall("d:response", ns):
        href = response.findtext("d:href", default="", namespaces=ns)
        statuses = [node.text or "" for node in response.findall("d:status", ns)]
        if any(len(status.split()) < 2 or status.split()[1] != "200" for status in statuses):
            raise WebDAVError("扫描未完成：WebDAV 目录包含不可读取的条目；原记录保持不变")
        prop = None
        for propstat in response.findall("d:propstat", ns):
            status = propstat.findtext("d:status", default="HTTP/1.1 200 OK", namespaces=ns)
            candidate = propstat.find("d:prop", ns)
            if (len(status.split()) >= 2 and status.split()[1] == "200"
                    and candidate is not None and candidate.find("d:resourcetype", ns) is not None):
                prop = candidate
                break
        if not href or prop is None:
            raise WebDAVError("扫描未完成：WebDAV 目录条目不完整；原记录保持不变")
        resource_type = prop.find("d:resourcetype", ns)
        is_collection = (
            resource_type is not None
            and resource_type.find("d:collection", ns) is not None
        )
        size_text = prop.findtext("d:getcontentlength", default="", namespaces=ns)
        try:
            size = int(size_text) if size_text else None
        except ValueError:
            size = None
        entries.append(
            RemoteEntry(
                path=unquote(urlsplit(href).path),
                is_collection=is_collection,
                etag=prop.findtext("d:getetag", default=None, namespaces=ns),
                last_modified=prop.findtext(
                    "d:getlastmodified", default=None, namespaces=ns
                ),
                size=size,
                content_type=prop.findtext(
                    "d:getcontenttype", default=None, namespaces=ns
                ),
            )
        )
    return entries
