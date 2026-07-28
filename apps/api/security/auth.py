"""Cookie-based session helpers for the M3-3 auth flow.

The session is a 32-byte URL-safe random token stored in an
HttpOnly ``SameSite=Lax`` cookie. The database only carries the
SHA-256 of the token so a database leak does not yield reusable
credentials.

The module is also responsible for the simple per-IP rate limiter
used by ``/api/auth/login`` and ``/api/auth/setup``: those endpoints
must throttle brute force without bringing in a third-party
dependency for a single-user app.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Iterable, Mapping


SESSION_COOKIE_NAME = "cangzhi_session"
SESSION_TTL = timedelta(hours=12)
TOKEN_BYTES = 32
MAX_SESSIONS_PER_ADMIN = 16


@dataclass(frozen=True)
class SessionRecord:
    """A minimal view of the session row used by the API layer."""

    session_id: int
    admin_id: int
    username: str
    expires_at: datetime


def hash_session_token(token: str) -> str:
    """Return the SHA-256 hex of ``token`` for storage."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """Return a fresh URL-safe session token."""

    return secrets.token_urlsafe(TOKEN_BYTES)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def expires_at_from_now(ttl: timedelta = SESSION_TTL) -> datetime:
    return now_utc() + ttl


# --- Rate limiter ---------------------------------------------------------


@dataclass
class _Bucket:
    """Rolling-window attempt log for a single key."""

    attempts: deque[float]

    def prune(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while self.attempts and self.attempts[0] < cutoff:
            self.attempts.popleft()


class RateLimiter:
    """A small in-memory rolling-window limiter.

    The default settings are tuned for ``/api/auth/login`` and
    ``/api/auth/setup``: 5 attempts per key per minute. Callers use
    an IP-and-account key for login and an IP key for setup. The limiter
    is process-local: a multi-process deployment would need a shared
    store, but M3-3 is single-user and runs the API as a single
    process per machine. Tests reset the limiter between cases.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lock = Lock()
        self._buckets: dict[str, _Bucket] = {}

    def hit(self, key: str) -> bool:
        """Register an attempt and return True if it is allowed."""

        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(attempts=deque())
                self._buckets[key] = bucket
            bucket.prune(now, self._window_seconds)
            if len(bucket.attempts) >= self._max_attempts:
                return False
            bucket.attempts.append(now)
            return True

    def remaining(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self._max_attempts
            bucket.prune(now, self._window_seconds)
            return max(self._max_attempts - len(bucket.attempts), 0)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# --- Cookie helpers -------------------------------------------------------


def cookie_attributes(*, secure: bool) -> dict[str, object]:
    """Return the keyword arguments used to set the session cookie.

    ``HttpOnly`` and ``SameSite=Lax`` are always on. ``Secure`` is
    only set when explicitly requested so local development over
    ``http://localhost`` keeps working; production deployments are
    expected to terminate TLS in front of the API.
    """

    attrs: dict[str, object] = {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
    }
    if secure:
        attrs["secure"] = True
    return attrs


def client_ip_from_headers(headers: Mapping[str, str]) -> str:
    """Best-effort client IP extraction for audit logging.

    Only the first ``X-Forwarded-For`` hop is honoured; the rest of
    the chain is discarded because every entry past the first is
    attacker-controlled. ``Forwarded`` (RFC 7239) is intentionally
    ignored: M3-3 only needs a coarse identifier for rate limiting
    and audit logging.
    """

    candidate = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if candidate:
        first = candidate.split(",")[0].strip()
        if first:
            return first[:64]
    real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if real_ip:
        cleaned = real_ip.strip()
        if cleaned:
            return cleaned[:64]
    return "unknown"


def user_agent_from_headers(headers: Mapping[str, str]) -> str:
    raw = headers.get("user-agent") or headers.get("User-Agent") or ""
    return raw.strip()[:512]


def is_secure_request(headers: Mapping[str, str]) -> bool:
    """Return True if the request arrived over a secure transport.

    Checks the ``X-Forwarded-Proto`` header so the API still marks
    cookies as ``Secure`` when fronted by a TLS-terminating proxy.
    """

    for key in ("x-forwarded-proto", "X-Forwarded-Proto"):
        value = headers.get(key)
        if value and value.lower().split(",")[0].strip() == "https":
            return True
    return False


def extract_bearer_token(headers: Mapping[str, str]) -> str:
    """Return a token from ``Authorization: Bearer <token>`` if any."""

    auth = headers.get("authorization") or headers.get("Authorization")
    if not auth:
        return ""
    parts = auth.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def normalised_cookie_iter(headers: Mapping[str, str]) -> Iterable[tuple[str, str]]:
    """Yield ``(name, value)`` pairs from a ``Cookie`` header."""

    raw = headers.get("cookie") or headers.get("Cookie")
    if not raw:
        return
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.strip().partition("=")
        if not name:
            continue
        yield name, value
