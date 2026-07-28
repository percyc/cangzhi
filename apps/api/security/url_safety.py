from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class URLSecurityError(ValueError):
    """Raised when a URL fails SSRF safety checks."""


@dataclass(frozen=True)
class AllowedAddress:
    """An address that has been validated as safe to connect to."""

    family: int
    address: str


def _normalize_scheme(scheme: str) -> str:
    return (scheme or "").lower()


def _ensure_safe_scheme(scheme: str) -> None:
    if _normalize_scheme(scheme) not in {"http", "https"}:
        raise URLSecurityError("仅支持 http 和 https 链接")


def _ensure_no_userinfo(netloc: str) -> None:
    if "@" in netloc:
        # Anything before the last '@' is treated as userinfo.
        raise URLSecurityError("链接中不允许包含用户信息")


def _safe_hostname(hostname: str) -> str:
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        raise URLSecurityError("链接缺少主机名")
    if any(ord(c) < 0x20 for c in host):
        raise URLSecurityError("链接主机名包含非法字符")
    if host == "localhost" or host.endswith(".localhost"):
        raise URLSecurityError("链接不能指向本机")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise URLSecurityError("链接主机名格式无效") from None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_is_blocked(ip.ipv4_mapped)
    # is_global excludes private, loopback, link-local, shared, reserved,
    # documentation, multicast and unspecified ranges on both IPv4 and IPv6.
    return not ip.is_global


def is_blocked_address(family: int, address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        # Unparseable literal — treat as unsafe.
        return True
    return _ip_is_blocked(ip)


def resolve_allowed_addresses(hostname: str) -> list[AllowedAddress]:
    """Resolve *hostname* to IP literals, blocking any unsafe result.

    Returns the list of safe addresses. If every result is unsafe (or the
    lookup fails) a ``URLSecurityError`` is raised.
    """
    host = _safe_hostname(hostname)

    # If the host already is a literal IP, validate it directly.
    try:
        literal = ipaddress.ip_address(host)
        if _ip_is_blocked(literal):
            raise URLSecurityError("链接指向受限 IP 地址")
        family = socket.AF_INET if literal.version == 4 else socket.AF_INET6
        return [AllowedAddress(family=family, address=str(literal))]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise URLSecurityError(f"无法解析主机名：{exc}") from None

    seen: set[tuple[int, str]] = set()
    allowed: list[AllowedAddress] = []
    for family, _, _, _, sockaddr in infos:
        address = sockaddr[0]
        key = (family, address)
        if key in seen:
            continue
        seen.add(key)
        if is_blocked_address(family, address):
            raise URLSecurityError("链接解析结果包含受限地址")
        allowed.append(AllowedAddress(family=family, address=address))

    if not allowed:
        raise URLSecurityError("链接解析结果为空")
    return allowed


def normalize_url(url: str) -> str:
    """Return a sanitized URL with default http/https ports stripped."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise URLSecurityError("链接端口格式无效") from None
    _ensure_safe_scheme(parts.scheme)
    _ensure_no_userinfo(parts.netloc)
    host = _safe_hostname(parts.hostname or "")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _ip_is_blocked(literal):
        raise URLSecurityError("链接指向受限 IP 地址")
    scheme = _normalize_scheme(parts.scheme)
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port in (None, default_port) else f"{rendered_host}:{port}"
    path = parts.path or "/"
    return parts._replace(
        scheme=scheme,
        netloc=netloc,
        path=path,
        fragment="",
    ).geturl()


def validate_redirect(target: str, previous: str) -> str:
    """Validate a redirect target before following it."""
    return normalize_url(target)
