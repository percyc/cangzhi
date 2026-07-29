from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit
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
) -> list[RemoteEntry]:
    safe_base = validate_webdav_url(
        base_url, trusted_private_network=trusted_private_network
    )
    path = "/" + (root_path or "/").strip("/")
    request_url = urljoin(safe_base, path.lstrip("/"))
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
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
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(
                "PROPFIND", request_url, headers=headers, content=body
            )
    except httpx.HTTPError as exc:
        raise WebDAVError(f"无法连接 WebDAV：{type(exc).__name__}") from exc
    if response.status_code not in {200, 207}:
        if response.status_code in {401, 403}:
            raise WebDAVError("WebDAV 认证失败，请检查用户名和密码")
        raise WebDAVError(f"WebDAV 返回 HTTP {response.status_code}")
    return parse_multistatus(response.content)


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
    for response in root.findall("d:response", ns):
        href = response.findtext("d:href", default="", namespaces=ns)
        prop = response.find("d:propstat/d:prop", ns)
        if not href or prop is None:
            continue
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
