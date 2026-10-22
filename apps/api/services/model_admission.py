"""Cross-process shared model-call admission (ADR-034).

Every function that actually sends a model request (chat / embedding / OCR)
takes one slot in a PostgreSQL-backed quota pool, holds a lease with a
unique owner token, and releases it only inside the real request's
``finally``.

Design invariants (ADR-034):
  * No network call while a DB transaction is held: acquire commits before
    the caller performs HTTP; release is a separate short CAS delete.
  * Lease TTL is strictly longer than the transport hard lifetime and lease
    timestamps are computed from the **database clock** (never an unsynced
    worker clock), so multi-process expiry is consistent.
  * Async transports run under a finite hard deadline via ``asyncio.wait_for``
    (cancellable); synchronous callers run the same async transport through
    ``run_sync`` (``asyncio.run``) so release happens inside the transport,
    not the outer await.
  * Fail closed when the coordination DB is unreachable.  Production always
    uses the Postgres backend; a test-only in-memory backend is gated on the
    test interpreter.
  * Busy / Unavailable / Cooldown are raised **before any request is sent**
    (at lease acquisition), so a caller can refund a pre-incremented attempt
    budget safely and defer without charging a retry count.
  * No layered retries: the bounded acquire and the 429 cooldown are typed
    delays; this module never retries the HTTP request.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..core.config import settings

logger = logging.getLogger(__name__)

# Purpose keys.
CHAT = "chat"
EMBEDDING = "embedding"
OCR = "ocr"
_PURPOSES = (CHAT, EMBEDDING, OCR)

FOREGROUND = "foreground"
BACKGROUND = "background"

# Default single shared pool id.
DEFAULT_POOL = "model"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Typed errors (fail closed, never mistaken for model/output failure)
# ---------------------------------------------------------------------------


class AdmissionError(RuntimeError):
    """Base: raised only at lease acquisition, i.e. **no request was sent**."""


class AdmissionBusyError(AdmissionError):
    """No free slot within the bounded acquire window (no request sent)."""


class AdmissionUnavailableError(AdmissionError):
    """Admission DB unreachable/table missing (fail closed, no request sent)."""


class AdmissionCooldownError(AdmissionError):
    """Pool cooling down from an upstream 429; no request sent."""

    def __init__(self, retry_after: float | None = None, message: str | None = None) -> None:
        super().__init__(message or "渠道正在临时冷却，请稍后再试")
        self.retry_after = retry_after


class AdmissionDeadlineError(AdmissionError):
    """The request's absolute hard deadline expired before it could be sent;
    no request was sent, so callers may defer without charging an attempt."""


@dataclass(frozen=True)
class Lease:
    """One held slot; ``owner_token`` uniquely identifies it for CAS release."""

    pool_id: str
    owner_token: str
    kind: str
    purpose: str
    lease_until: datetime


# ---------------------------------------------------------------------------
# Config (reads the non-secret env settings; falls back to safe defaults)
# ---------------------------------------------------------------------------


class _Config:
    def __init__(self) -> None:
        self.capacity = min(128, max(1, int(settings.model_admission_capacity)))
        self.background_capacity = min(self.capacity, max(0, int(settings.model_admission_background_capacity)))
        self.max_ocr = min(self.capacity, max(1, int(settings.model_admission_max_ocr)))
        self.lease_padding = max(1.0, float(settings.model_admission_lease_padding_seconds))
        self.acquire_timeout = max(0.05, float(settings.model_admission_acquire_timeout_seconds))
        self.acquire_poll = max(0.01, float(settings.model_admission_acquire_poll_seconds))
        self.retry_min = max(1.0, float(settings.model_admission_retry_min_seconds))
        raw_map = (settings.model_admission_pool_map or "").strip()
        self.pool_map: dict[str, str] = {}
        if raw_map:
            try:
                parsed = json.loads(raw_map)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                for purpose, pid in parsed.items():
                    if purpose in _PURPOSES and isinstance(pid, str) and 0 < len(pid.strip()) <= 64:
                        self.pool_map[purpose] = pid.strip()


def _config() -> _Config:
    return _Config()


def channel_pool_id(purpose: str) -> str:
    """Resolve the configured pool id for a purpose (optional sharing map).

    Default: every purpose shares the single conservative pool
    (``model``) so a shared upstream never sees multiplied limits.
    """
    cfg = _config()
    return cfg.pool_map.get(purpose, DEFAULT_POOL)


def _default_capacity(pool_id: str) -> tuple[int, int, int]:
    cfg = _config()
    ocr = cfg.max_ocr
    # All pools keep the same conservative total/background defaults; only
    # the OCR sub-limit is specialised.  (Unless an operator tunes the DB row.)
    return (cfg.capacity, cfg.background_capacity, ocr)


# ---------------------------------------------------------------------------
# Process role / background classification
# ---------------------------------------------------------------------------

_process_role = FOREGROUND
_role_lock = threading.Lock()


def set_process_role(role: str) -> None:
    """Mark this process ``foreground`` (default) or ``background`` (worker).

    Both ``"worker"`` and ``"background"`` are treated as background so the
    worker startup and job-execution entry can classify all model calls as
    background without each call site opting in.
    """
    global _process_role
    with _role_lock:
        _process_role = BACKGROUND if role in (BACKGROUND, "worker") else FOREGROUND


def current_kind() -> str:
    with _role_lock:
        return _process_role


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class AdmissionBackend:
    def acquire(
        self,
        pool_id: str,
        kind: str,
        purpose: str,
        *,
        lease_ttl_seconds: float,
        acquire_timeout_seconds: float,
        now: datetime | None = None,
    ) -> Lease:
        raise NotImplementedError

    def release(self, lease: Lease) -> bool:
        raise NotImplementedError

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        raise NotImplementedError

    def cooldown_remaining(self, pool_id: str) -> float:
        raise NotImplementedError


class PostgresAdmissionBackend(AdmissionBackend):
    """Cooperative cross-process backend using short, bounded PostgreSQL
    transactions and the database clock for lease expiry."""

    def __init__(self, url: str | None = None, engine: Any | None = None) -> None:
        resolved = (url or settings.database_url)
        if resolved.startswith("postgres://"):
            resolved = "postgresql://" + resolved[len("postgres://"):]
        connect_args = {}
        if resolved.startswith("postgresql"):
            connect_args = {"connect_timeout": 3}
        self._engine: Engine = engine if engine is not None else create_engine(
            resolved,
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            pool_timeout=3,
            connect_args=connect_args,
            future=True,
        )

    def _ensure_pool(self, conn, pool_id: str) -> None:
        cap, bg, ocr = _default_capacity(pool_id)
        conn.execute(
            text(
                "INSERT INTO model_admission_pools "
                "(pool_id, capacity, background_capacity, ocr_sublimit, cooldown_until, cooldown_retry_after) "
                "VALUES (:pid, :cap, :bg, :ocr, NULL, NULL) "
                "ON CONFLICT (pool_id) DO NOTHING"
            ),
            {"pid": pool_id, "cap": cap, "bg": bg, "ocr": ocr},
        )

    def _set_timeout(self, conn) -> None:
        # Bound the connect/statement/lock-wait window inside any PG transaction
        # so a stuck coordinator cannot stall an admission call indefinitely.
        if self._is_pg():
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            conn.execute(text("SET LOCAL lock_timeout = 1000"))

    def _try_acquire(self, *, pool_id: str, kind: str, purpose: str,
                     lease_ttl_seconds: float) -> tuple[str, Any, datetime | None]:
        token = uuid.uuid4().hex
        ttl = float(lease_ttl_seconds)
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            self._ensure_pool(conn, pool_id)
            pool = conn.execute(
                text(
                    "SELECT capacity, background_capacity, ocr_sublimit, cooldown_until, "
                    "cooldown_retry_after FROM model_admission_pools WHERE pool_id = :pid FOR UPDATE"
                ),
                {"pid": pool_id},
            ).mappings().one_or_none()
            if pool is None:
                return "unavailable", None, None
            db_now = self._db_now(conn)
            if pool["cooldown_until"] is not None and pool["cooldown_until"] > db_now:
                return "cooldown", pool["cooldown_retry_after"], db_now
            # Reap expired leases for this pool inside the same transaction.
            conn.execute(
                text("DELETE FROM model_admission_leases WHERE pool_id = :pid AND lease_until <= :ts"),
                {"pid": pool_id, "ts": db_now},
            )
            total = conn.execute(
                text("SELECT COUNT(*) FROM model_admission_leases "
                     "WHERE pool_id = :pid AND lease_until > :ts"),
                {"pid": pool_id, "ts": db_now},
            ).scalar_one()
            bg = conn.execute(
                text("SELECT COUNT(*) FROM model_admission_leases "
                     "WHERE pool_id = :pid AND kind = 'background' AND lease_until > :ts"),
                {"pid": pool_id, "ts": db_now},
            ).scalar_one()
            ocr = 0
            if purpose == OCR:
                ocr = conn.execute(
                    text("SELECT COUNT(*) FROM model_admission_leases "
                         "WHERE pool_id = :pid AND purpose = 'ocr' AND lease_until > :ts"),
                    {"pid": pool_id, "ts": db_now},
                ).scalar_one()
            cap, bg_cap, ocr_cap = pool["capacity"], pool["background_capacity"], pool["ocr_sublimit"]
            if total >= cap or (kind == BACKGROUND and bg >= bg_cap) or (purpose == OCR and ocr >= ocr_cap):
                return "busy", None, db_now
            until = db_now + timedelta(seconds=ttl)
            conn.execute(
                text("INSERT INTO model_admission_leases "
                     "(pool_id, owner_token, kind, purpose, lease_until) "
                     "VALUES (:pid, :token, :kind, :purpose, :until)"),
                {"pid": pool_id, "token": token, "kind": kind, "purpose": purpose, "until": until},
            )
            return "ok", token, until

    def _db_now(self, conn) -> datetime:
        if self._is_pg():
            return conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        return _now()

    def _is_pg(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def acquire(self, pool_id, kind, purpose, *, lease_ttl_seconds, acquire_timeout_seconds, now=None):
        deadline_mono = time.monotonic() + acquire_timeout_seconds
        while True:
            try:
                status, payload, until = self._try_acquire(
                    pool_id=pool_id, kind=kind, purpose=purpose, lease_ttl_seconds=lease_ttl_seconds
                )
            except AdmissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - DB outage must fail closed
                logger.warning("model_admission_db_unavailable pool=%s err=%s", pool_id, type(exc).__name__)
                raise AdmissionUnavailableError("模型准入数据库不可用") from exc
            if status == "ok":
                return Lease(pool_id=pool_id, owner_token=payload, kind=kind,
                             purpose=purpose, lease_until=until or (_now() + timedelta(seconds=lease_ttl_seconds)))
            if status == "cooldown":
                retry_after = float(payload) if payload else None
                raise AdmissionCooldownError(retry_after=retry_after or None,
                                             message="渠道正在临时冷却，请稍后再试")
            if status == "unavailable":
                raise AdmissionUnavailableError("模型准入表不可用")
            if time.monotonic() >= deadline_mono:
                raise AdmissionBusyError("模型额度繁忙，短时间无法获取")
            time.sleep(_config().acquire_poll)

    def release(self, lease: Lease) -> bool:
        if lease is None:
            return True
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            result = conn.execute(
                text("DELETE FROM model_admission_leases WHERE pool_id = :pid AND owner_token = :token"),
                {"pid": lease.pool_id, "token": lease.owner_token},
            )
            return (result.rowcount or 0) > 0

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            self._ensure_pool(conn, pool_id)
            now = self._db_now(conn)
            seconds = min(3600.0, max(1.0, float(cooldown_seconds)))
            until = now + timedelta(seconds=seconds)
            conn.execute(
                text("UPDATE model_admission_pools SET cooldown_until = GREATEST(cooldown_until, :until), "
                     "cooldown_retry_after = :ra, updated_at = :now WHERE pool_id = :pid"),
                {"pid": pool_id, "until": until, "ra": int(min(3600, max(1, retry_after or seconds))), "now": now},
            )

    def cooldown_remaining(self, pool_id: str) -> float:
        with self._engine.connect() as conn:
            self._set_timeout(conn)
            row = conn.execute(
                text("SELECT cooldown_until FROM model_admission_pools WHERE pool_id = :pid"),
                {"pid": pool_id},
            ).mappings().one_or_none()
            now = self._db_now(conn)
        if row is None or row["cooldown_until"] is None:
            return 0.0
        return max(0.0, (row["cooldown_until"] - now).total_seconds())


class InMemoryAdmissionBackend(AdmissionBackend):
    """Test-only single-process backend (gated on the test interpreter).

    Mirrors the same lease / owner / CAS semantics and OCR sub-limit, using a
    real ``threading.Lock`` so it is correct even under the threaded sync
    facades used in unit tests, with no live Postgres.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.leases: dict[str, tuple[str, str, str, datetime]] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.cooldown_retry_after: dict[str, float] = {}

    def _prune(self, now: datetime) -> list[tuple[str, str, str]]:
        live: list[tuple[str, str, str]] = []
        for token, (pid, k, purpose, until) in list(self.leases.items()):
            if until <= now:
                self.leases.pop(token, None)
            else:
                live.append((pid, k, purpose))
        return live

    def acquire(self, pool_id, kind, purpose, *, lease_ttl_seconds, acquire_timeout_seconds, now=None):
        deadline_mono = time.monotonic() + acquire_timeout_seconds
        while True:
            with self._lock:
                now = _now()
                if self.cooldown_until.get(pool_id, now) > now:
                    raise AdmissionCooldownError(retry_after=self.cooldown_retry_after.get(pool_id))
                live = [entry for entry in self._prune(now) if entry[0] == pool_id]
                cap, bg_cap, ocr_cap = _default_capacity(pool_id)
                total = len(live)
                bgcount = sum(1 for _, k, _ in live if k == BACKGROUND)
                ocr_count = sum(1 for _, _, p in live if p == OCR)
                if (total < cap
                        and not (kind == BACKGROUND and bgcount >= bg_cap)
                        and not (purpose == OCR and ocr_count >= ocr_cap)):
                    token = uuid.uuid4().hex
                    self.leases[token] = (pool_id, kind, purpose, now + timedelta(seconds=lease_ttl_seconds))
                    return Lease(pool_id=pool_id, owner_token=token, kind=kind,
                                 purpose=purpose, lease_until=now + timedelta(seconds=lease_ttl_seconds))
            if time.monotonic() >= deadline_mono:
                raise AdmissionBusyError("模型额度繁忙")
            time.sleep(0.01)

    def release(self, lease: Lease) -> bool:
        if lease is None:
            return True
        with self._lock:
            held = self.leases.get(lease.owner_token)
            if held is None or held[0] != lease.pool_id:
                return False
            del self.leases[lease.owner_token]
            return True

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        with self._lock:
            until = _now() + timedelta(seconds=min(3600, max(1, cooldown_seconds)))
            self.cooldown_until[pool_id] = max(self.cooldown_until.get(pool_id, until), until)
            self.cooldown_retry_after[pool_id] = float(retry_after) if retry_after else float(cooldown_seconds)

    def cooldown_remaining(self, pool_id: str) -> float:
        with self._lock:
            until = self.cooldown_until.get(pool_id)
            if until is None:
                return 0.0
            return max(0.0, (until - _now()).total_seconds())


# ---------------------------------------------------------------------------
# Module singleton + (test-only) injection
# ---------------------------------------------------------------------------

_backend: AdmissionBackend | None = None
_test_injected: AdmissionBackend | None = None


def configure_admission_backend(backend: AdmissionBackend | None) -> None:
    """Inject a backend for tests; ``None`` resets to production Postgres.

    Only permitted in the test interpreter so a production process can never
    silently disable coordination.  Production code never calls this.
    """
    if "pytest" not in sys.modules:
        raise RuntimeError("admission backend injection is test-only")
    global _test_injected
    _test_injected = backend


def reset_admission_backend() -> None:
    global _test_injected
    _test_injected = None


def admission_backend() -> AdmissionBackend:
    global _backend
    if _test_injected is not None:
        return _test_injected
    if _backend is None:
        _backend = PostgresAdmissionBackend()
    return _backend


def apply_cooldown(pool_id: str, cooldown_seconds: float, retry_after: float | None = None) -> None:
    admission_backend().apply_cooldown(pool_id, cooldown_seconds, retry_after)


def cooldown_remaining(pool_id: str) -> float:
    return admission_backend().cooldown_remaining(pool_id)


# ---------------------------------------------------------------------------
# Upstream-status cooldown
# ---------------------------------------------------------------------------
# A single shared pool means one upstream talks to the whole product. When it
# answers 429 the pool cools down with the caller-supplied Retry-After;
# 401/403 and transient 5xx add small bounded cooldowns so a revoked or broken
# channel is not hot-spammed from every worker process. None of these record a
# URL or key, and they never mask the *current* real request failure, which the
# caller is still expected to report truthfully through its own error path.

_AUTH_COOLDOWN_SECONDS = 300.0
_TRANSIENT_COOLDOWN_SECONDS = 60.0
_DEFAULT_RETRY_AFTER = 60.0


def _bounded_retry_after(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return min(3600.0, max(1.0, float(value)))


def parse_retry_after(headers) -> float | None:
    """Parse a ``Retry-After`` header into bounded seconds.

    Supports both the delta-seconds form and the HTTP-date form, is
    NaN/Inf-safe, and returns ``None`` when the header is absent or
    cannot be interpreted. Extremely large values are clamped.
    """
    if not headers:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # HTTP-date form, e.g. "Wed, 21 Oct 2015 07:28:00 GMT".
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed is None:
            return None
        delta = (parsed.astimezone(timezone.utc) - _now()).total_seconds()
        return _bounded_retry_after(delta)
    if math.isnan(value) or math.isinf(value) or value <= 0:
        return None
    return _bounded_retry_after(value)


def apply_response_cooldown(
    pool_id: str, status_code: int, retry_after: float | None = None
) -> None:
    """Apply a bounded pool cooldown derived from an upstream HTTP status.

    * ``429`` honours ``Retry-After`` (falling back to a conservative 60 s).
    * ``401/403`` add a short auth-failure cooldown so a revoked credential is
      not retried from every process.
    * ``5xx`` add a small transient cooldown.

    The current request's real failure is left untouched; callers must still
    surface it via their own error handling.
    """
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return
    ra = _bounded_retry_after(retry_after)
    if status == 429:
        apply_cooldown(pool_id, ra or _DEFAULT_RETRY_AFTER, retry_after=ra)
    elif status in (401, 403):
        apply_cooldown(pool_id, _AUTH_COOLDOWN_SECONDS)
    elif 500 <= status <= 599:
        apply_cooldown(pool_id, _TRANSIENT_COOLDOWN_SECONDS)


# ---------------------------------------------------------------------------
# Async channel (total hard deadline, shielded acquire/release, cancellable)
# ---------------------------------------------------------------------------


def _hard_and_ttl(hard_lifetime_seconds: float | None) -> tuple[float, float]:
    cfg = _config()
    hard = max(1.0, float(hard_lifetime_seconds or 35.0))
    if not math.isfinite(hard) or not math.isfinite(cfg.lease_padding):
        raise ValueError("model request lifetime must be finite")
    return hard, hard + cfg.lease_padding


class _AsyncChannel:
    """A hold on one admission slot plus an *absolute monotonic* hard deadline
    captured before the acquire so a delayed start or several posts in the same
    channel can never run beyond ``lease_until``."""

    def __init__(
        self,
        backend: AdmissionBackend,
        deadline_monotonic: float,
        lease_until: datetime,
    ) -> None:
        self._backend = backend
        self._deadline = deadline_monotonic
        self._lease_until = lease_until
        self._started = False

    async def run(self, awaitable: "Awaitable[Any] | asyncio.Future") -> Any:
        """Run one outbound awaitable under the remaining hard deadline.

        ``remaining`` is derived from the absolute deadline captured before the
        acquire, so no single invocation (nor a delayed/multi-call sequence) can
        push the request past the lease's ``lease_until``.  If the deadline is
        already expired, the not-yet-started awaitable is closed (never sent)
        and a typed :class:`AdmissionDeadlineError` is raised so callers defer
        without charging an attempt.
        """
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            close = getattr(awaitable, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - defensive close
                    pass
            if self._started:
                raise TimeoutError("模型调用超出总期限")
            raise AdmissionDeadlineError("模型调用超出硬期限，未发送")
        self._started = True
        return await asyncio.wait_for(awaitable, timeout=remaining)


async def _settle(task: asyncio.Future) -> tuple[Any, bool]:
    """Finish bounded DB work even under repeated cancellation.

    A callback on an asyncio Task can be lost when its event loop closes.
    Waiting for the short DB operation keeps ownership explicit instead.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _release(backend: AdmissionBackend, lease: Lease) -> bool:
    def release_safely():
        try:
            backend.release(lease)
        except Exception:
            # Keep the lease until expiry on coordinator failure, without
            # discarding a successful model response and causing a paid replay.
            logger.warning("model_admission_release_failed pool=%s", lease.pool_id)
    _, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(release_safely)))
    return cancelled


@asynccontextmanager
async def async_channel(
    purpose: str,
    kind: str | None = None,
    hard_lifetime_seconds: float | None = None,
    pool_id: str | None = None,
) -> AsyncIterator["_AsyncChannel"]:
    """Acquire a slot (before any request), yield a channel whose ``run``
    enforces ``asyncio.wait_for`` hard deadline (cancellable), and release
    **only in the ``finally`` after the transport body exits** so the slot is
    held for the entire request.  The threaded acquire is shielded; a
    cancellation during a still-running acquire waits for its bounded result
    and releases any acquired lease before propagating cancellation."""
    hard, ttl = _hard_and_ttl(hard_lifetime_seconds)
    pid = pool_id or channel_pool_id(purpose)
    backend = admission_backend()
    # Absolute monotonic deadline captured BEFORE the acquire: the run window
    # starts here, so a delayed acquire can never push the request past the
    # lease's ``lease_until`` (ttl == hard + padding).
    deadline = time.monotonic() + hard
    acquire_fut = asyncio.ensure_future(
        asyncio.to_thread(
            backend.acquire, pid, kind or current_kind(), purpose,
            lease_ttl_seconds=ttl, acquire_timeout_seconds=_config().acquire_timeout,
        )
    )
    try:
        lease = await asyncio.shield(acquire_fut)
    except asyncio.CancelledError:
        try:
            late_lease, _ = await _settle(acquire_fut)
        except Exception:
            pass  # No lease was obtained.
        else:
            await _release(backend, late_lease)
        raise
    if isinstance(lease, AdmissionError):
        raise lease
    try:
        yield _AsyncChannel(backend=backend, deadline_monotonic=deadline, lease_until=lease.lease_until)
    finally:
        # Create + await the release here, AFTER the request exited, so the
        # slot is never dropped early.  Shield it: cancellation must not drop it.
        if await _release(backend, lease):
            raise asyncio.CancelledError()


def run_sync(awaitable_factory: Callable[[], "Awaitable[Any]"]) -> Any:
    """Run an async transport (with its admission channel and hard deadline)
    to completion in a fresh event loop.  The lease is released inside the
    transport's own async ``finally``; if an outer ``asyncio.to_thread`` is
    cancelled, this thread keeps running to completion so we never release a
    slot while the real request is still in flight."""
    return asyncio.run(awaitable_factory())
