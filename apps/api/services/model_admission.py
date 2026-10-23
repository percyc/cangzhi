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

Failure isolation (ADR-034 follow-up, 0044):

  A single shared pool means one upstream speaks for the whole product, but a
  5xx from *one* model/credential must not freeze every other foreground ask.
  Cooldowns are therefore routed in two tiers:

    * ``429`` stays **global** and conservative: the upstream itself is asking
      us to back off, and we have no evidence about which model is at fault.
    * ``401/403/5xx`` become **scoped** when the call site supplies an opaque
      ``failure_scope``. Without a scope a 5xx keeps the historical global
      behaviour (documented compatibility, not an oversight).

  A scope is an irreversible one-way digest of
  ``(purpose, endpoint, model, credential)``. Raw values are never stored, so
  the circuit state cannot leak a channel address or key.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import logging
import math
import re
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

# Reserved prefix for the scoped circuit-breaker rows that reuse the
# ``model_admission_pools`` table.  A scope pool never counts toward the real
# pool's capacity: a scoped probe is taken *in addition to* the shared lease.
SCOPE_POOL_PREFIX = "scope:"

# Scope digests are fixed-length hex so they can be validated before touching
# SQL (defence against an injected value reaching a query parameter).  56 hex
# chars keeps ``scope:`` + digest (62) inside the VARCHAR(64) ``pool_id``.
_SCOPE_RE = re.compile(r"^[0-9a-f]{56}$")
_SCOPE_HEX_CHARS = 56


def _scope_part(value: object) -> str:
    """Render one scope component losslessly; never log the result.

    Model names and credentials are **case sensitive**, so no lowercasing or
    stripping is applied here: ``sk-AbC`` and ``sk-abc`` are different keys and
    must never share a circuit.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    # Non-string inputs (e.g. an int model id) are stringified so the digest
    # stays deterministic across processes.
    return str(value)


def failure_scope(purpose: str, endpoint: str, model: str, credential: str = "") -> str:
    """Return an opaque, stable failure-isolation scope for one channel.

    The result is a 56-char lowercase hex SHA-256 digest over the canonical
    JSON array ``[purpose, endpoint, model, credential]``.  A JSON array (not a
    delimiter join) is used so no combination of component values can collide by
    shifting content across the separator, and no component is normalised:
    model identifiers and API keys stay case sensitive.

    The digest is safe to persist and to log - it cannot be reversed into the
    channel URL, model name or key, and it is identical in every process, so
    two workers isolate the *same* channel together.

    Callers pass the real values here only; the digest is what leaves this
    function.  An empty endpoint and empty credential are still hashed, so a
    missing credential never collapses into a shared, easily-tripped scope.
    """
    canonical = json.dumps(
        [_scope_part(purpose), _scope_part(endpoint), _scope_part(model), _scope_part(credential)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_SCOPE_HEX_CHARS]


def is_failure_scope(value: object) -> bool:
    """Validate a caller-supplied scope before it is used in a query."""
    return isinstance(value, str) and bool(_SCOPE_RE.match(value))


def _scope_pool_id(scope: str) -> str:
    return f"{SCOPE_POOL_PREFIX}{scope}"


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
class Probe:
    """One half-open probe this process owns.

    ``expected_until`` is the CAS token identifying the circuit generation the
    probe belongs to: a success may close the circuit only while the row still
    carries that marker, so a newer failure is never erased by a late success.
    """

    pool_id: str
    token: str
    expected_until: datetime | None


@dataclass(frozen=True)
class HeldLease:
    """Backend-internal acquire result: the real lease plus any probe claims."""

    token: str
    scope: str | None
    probes: tuple[Probe, ...]
    until: datetime


@dataclass(frozen=True)
class Lease:
    """One held slot; ``owner_token`` uniquely identifies it for CAS release."""

    pool_id: str
    owner_token: str
    kind: str
    purpose: str
    lease_until: datetime
    failure_scope: str | None = None
    probes: tuple[Probe, ...] = ()


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
                    if (purpose in _PURPOSES and isinstance(pid, str)
                            and 0 < len(pid.strip()) <= 64
                            and not pid.strip().startswith(SCOPE_POOL_PREFIX)):
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
        failure_scope: str | None = None,
    ) -> Lease:
        raise NotImplementedError

    def release(self, lease: Lease) -> bool:
        raise NotImplementedError

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        raise NotImplementedError

    def cooldown_remaining(self, pool_id: str) -> float:
        raise NotImplementedError

    # -- failure isolation -------------------------------------------------

    def apply_scope_cooldown(self, pool_id: str, scope: str, cooldown_seconds: float,
                             retry_after: float | None = None) -> None:
        """Open (or extend) the circuit for one opaque channel scope."""
        raise NotImplementedError

    def clear_probe_circuit(self, probe: "Probe") -> bool:
        """Close the circuit a probe belongs to, only while that probe is valid.

        Ownership is proved by the probe's own ``owner_token`` still being held
        and unexpired, not merely by a cooldown timestamp.  That closes two
        holes at once: a late success from a request that predates the failure
        cannot clear anything, and an expired probe cannot clear the *newer*
        generation's circuit.  Returns whether the circuit was actually cleared.
        """
        raise NotImplementedError

    def scope_cooldown_remaining(self, pool_id: str, scope: str) -> float:
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

    def _ensure_scope_pool(self, conn, scope: str) -> None:
        """Create the scope's circuit row if absent.

        A scope row is a ``model_admission_pools`` row with the reserved
        ``scope:`` prefix.  Its capacity is 1 because it only ever represents
        a single in-flight probe, never real quota, so operators can exclude
        these rows from capacity reporting with a prefix filter.
        """
        sid = _scope_pool_id(scope)
        conn.execute(
            text(
                "INSERT INTO model_admission_pools "
                "(pool_id, capacity, background_capacity, ocr_sublimit, cooldown_until, cooldown_retry_after) "
                "VALUES (:sid, 1, 1, 1, NULL, NULL) "
                "ON CONFLICT (pool_id) DO NOTHING"
            ),
            {"sid": sid},
        )

    def _claim_probe(
        self,
        conn,
        circuit_pool_id: str,
        *,
        scope: str | None,
        lease_ttl_seconds: float,
    ) -> tuple[str, str, float | None, datetime | None]:
        """Decide whether this caller may run the single half-open probe.

        ``circuit_pool_id`` is either the real shared pool (global 429) or a
        ``scope:`` row.  The circuit row is locked ``FOR UPDATE``, so across
        processes exactly one caller observes the elapsed cooldown and takes the
        probe.

        The probe is a *lease* row on the circuit pool with ``kind='probe'`` and
        the **full** request TTL (never a shortened cap, so a 600 s request
        cannot be cut short), giving a crashed prober a finite, self-healing
        reservation instead of a permanently open circuit.

        ``cooldown_until`` is deliberately **not** advanced here.  Leaving the
        elapsed timestamp in place is what makes the CAS correct: a newer
        failure rewrites it to a *future* value, so a later success - whose
        expected value is the old elapsed one - no longer matches and cannot
        erase the newer failure.

        Returns ``(status, probe_token, retry_after, expected_until)``:

        * ``("probe", token, retry_after, elapsed_until)`` - this caller owns
          the probe; ``elapsed_until`` is the CAS token for a later success.
        * ``("cooling", "", retry_after, None)`` - still cooling, or another
          process currently holds the single probe.
        * ``("closed", "", None, None)`` - the circuit is already clear.
        """
        row = conn.execute(
            text(
                "SELECT cooldown_until, cooldown_retry_after FROM model_admission_pools "
                "WHERE pool_id = :cid FOR UPDATE"
            ),
            {"cid": circuit_pool_id},
        ).mappings().one_or_none()
        if row is None:
            return "closed", "", None, None
        db_now = self._db_now(conn)
        conn.execute(
            text("DELETE FROM model_admission_leases WHERE pool_id = :cid "
                 "AND kind = 'probe' AND lease_until <= :ts"),
            {"cid": circuit_pool_id, "ts": db_now},
        )
        until = row["cooldown_until"]
        if until is None:
            return "closed", "", None, None
        retry_after = float(row["cooldown_retry_after"]) if row["cooldown_retry_after"] else None
        if until > db_now:
            return "cooling", "", retry_after, None
        # Elapsed -> half-open. At most one probe may be in flight, so an
        # existing live probe lease makes every other caller back off.
        live_probe = conn.execute(
            text(
                "SELECT 1 FROM model_admission_leases WHERE pool_id = :cid "
                "AND kind = 'probe' AND lease_until > :ts LIMIT 1"
            ),
            {"cid": circuit_pool_id, "ts": db_now},
        ).first()
        if live_probe is not None:
            return "cooling", "", retry_after, None
        probe_token = uuid.uuid4().hex
        conn.execute(
            text(
                "INSERT INTO model_admission_leases "
                "(pool_id, owner_token, kind, purpose, failure_scope, lease_until) "
                "VALUES (:cid, :token, 'probe', 'probe', :scope, :until)"
            ),
            {
                "cid": circuit_pool_id,
                "token": probe_token,
                "scope": scope,
                "until": db_now + timedelta(seconds=float(lease_ttl_seconds)),
            },
        )
        return "probe", probe_token, retry_after, until

    def _set_timeout(self, conn) -> None:
        # Bound the connect/statement/lock-wait window inside any PG transaction
        # so a stuck coordinator cannot stall an admission call indefinitely.
        if self._is_pg():
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            conn.execute(text("SET LOCAL lock_timeout = 1000"))

    def _try_acquire(self, *, pool_id: str, kind: str, purpose: str,
                     lease_ttl_seconds: float,
                     failure_scope: str | None = None) -> tuple[str, Any, datetime | None]:
        token = uuid.uuid4().hex
        ttl = float(lease_ttl_seconds)
        scope = failure_scope if is_failure_scope(failure_scope) else None
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            self._ensure_pool(conn, pool_id)
            if scope:
                self._ensure_scope_pool(conn, scope)
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

            # Gate 1: the global shared-pool circuit. A 429 is the upstream
            # asking us to back off, so it stays conservative and global - but
            # once the cooldown has elapsed exactly one caller may probe.
            probes: list[Probe] = []
            for circuit in ([pool_id] + ([_scope_pool_id(scope)] if scope else [])):
                status, probe_token, retry_after, expected = self._claim_probe(
                    conn, circuit, scope=scope if circuit != pool_id else None,
                    lease_ttl_seconds=ttl,
                )
                if status == "cooling":
                    # Returning from inside ``begin`` COMMITS, it does not roll
                    # back, so any probe already taken in this transaction must
                    # be released explicitly before we return.
                    self._abandon_probes(conn, probes)
                    return ("cooldown" if circuit == pool_id else "scope_cooldown",
                            retry_after, expected)
                if status == "probe":
                    probes.append(Probe(circuit, probe_token, expected))

            # Reap expired leases for this pool inside the same transaction.
            conn.execute(
                text("DELETE FROM model_admission_leases WHERE pool_id = :pid AND lease_until <= :ts"),
                {"pid": pool_id, "ts": db_now},
            )
            # Probe leases (``kind='probe'``) never count toward quota: they
            # ride on the real slot below. A pool of capacity 1 must still be
            # able to half-open recover.
            total = conn.execute(
                text("SELECT COUNT(*) FROM model_admission_leases "
                     "WHERE pool_id = :pid AND kind <> 'probe' AND lease_until > :ts"),
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
            # Background work must never be able to consume the last slot of a
            # pool, so an interactive foreground call always keeps one free.
            # With capacity 1 there is nothing to reserve: background work is
            # still allowed while the pool is empty (no fairness or preemption
            # is promised in that configuration).
            bg_effective = min(bg_cap, max(0, cap - 1)) if cap > 1 else bg_cap
            if total >= cap or (kind == BACKGROUND and bg >= bg_effective) or (
                purpose == OCR and ocr >= ocr_cap
            ):
                self._abandon_probes(conn, probes)
                return "busy", None, db_now
            until = db_now + timedelta(seconds=ttl)
            conn.execute(
                text("INSERT INTO model_admission_leases "
                     "(pool_id, owner_token, kind, purpose, failure_scope, lease_until) "
                     "VALUES (:pid, :token, :kind, :purpose, :scope, :until)"),
                {"pid": pool_id, "token": token, "kind": kind, "purpose": purpose,
                 "scope": scope, "until": until},
            )
            return "ok", HeldLease(token=token, scope=scope, probes=tuple(probes), until=until), until

    @staticmethod
    def _abandon_probes(conn, probes: list["Probe"]) -> None:
        """Drop probe leases taken earlier in this transaction.

        If the real slot turns out to be busy, the half-open probe must not be
        consumed: the circuit stays in its observed (elapsed) state so the next
        caller can take it instead of burning the single probe on a request
        that never left the process.  Deleting by owner token only ever touches
        this caller's own probe.
        """
        for probe in probes:
            conn.execute(
                text("DELETE FROM model_admission_leases WHERE pool_id = :cid "
                     "AND owner_token = :token"),
                {"cid": probe.pool_id, "token": probe.token},
            )

    def _db_now(self, conn) -> datetime:
        if self._is_pg():
            return conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        return _now()

    def _is_pg(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def acquire(self, pool_id, kind, purpose, *, lease_ttl_seconds, acquire_timeout_seconds,
                now=None, failure_scope=None):
        deadline_mono = time.monotonic() + acquire_timeout_seconds
        while True:
            try:
                status, payload, until = self._try_acquire(
                    pool_id=pool_id, kind=kind, purpose=purpose,
                    lease_ttl_seconds=lease_ttl_seconds, failure_scope=failure_scope,
                )
            except AdmissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - DB outage must fail closed
                logger.warning("model_admission_db_unavailable pool=%s err=%s", pool_id, type(exc).__name__)
                raise AdmissionUnavailableError("模型准入数据库不可用") from exc
            if status == "ok":
                held: HeldLease = payload
                return Lease(pool_id=pool_id, owner_token=held.token, kind=kind,
                             purpose=purpose,
                             lease_until=until or (_now() + timedelta(seconds=lease_ttl_seconds)),
                             failure_scope=held.scope, probes=held.probes)
            if status in ("cooldown", "scope_cooldown"):
                retry_after = float(payload) if payload else None
                raise AdmissionCooldownError(
                    retry_after=retry_after or None,
                    message="渠道正在临时冷却，请稍后再试",
                )
            if status == "unavailable":
                raise AdmissionUnavailableError("模型准入表不可用")
            if time.monotonic() >= deadline_mono:
                raise AdmissionBusyError("模型额度繁忙，短时间无法获取")
            time.sleep(_config().acquire_poll)

    def release(self, lease: Lease) -> bool:
        """Release the caller's real slot and any probe it still owns.

        Both deletes are scoped by the caller's own ``owner_token``, so this
        can never remove a successor's lease.  Probe rows are removed with the
        real slot: a completed request no longer needs the circuit reserved.
        """
        if lease is None:
            return True
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            result = conn.execute(
                text("DELETE FROM model_admission_leases WHERE pool_id = :pid AND owner_token = :token"),
                {"pid": lease.pool_id, "token": lease.owner_token},
            )
            for probe in lease.probes:
                conn.execute(
                    text("DELETE FROM model_admission_leases WHERE pool_id = :pid AND owner_token = :token"),
                    {"pid": probe.pool_id, "token": probe.token},
                )
            return (result.rowcount or 0) > 0

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        self._apply_circuit(pool_id, cooldown_seconds, retry_after, scope=None)

    def apply_scope_cooldown(self, pool_id: str, scope: str, cooldown_seconds: float,
                             retry_after: float | None = None) -> None:
        if not is_failure_scope(scope):
            raise ValueError("invalid failure scope")
        self._apply_circuit(pool_id, cooldown_seconds, retry_after, scope=scope)

    def _apply_circuit(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None, *, scope: str | None) -> None:
        """Open or extend a circuit, never shortening a longer live cooldown.

        ``pool_id`` is the *real* shared pool (global) and ``scope`` selects the
        destination row: ``None`` means the pool row itself, otherwise the
        scope's own ``scope:`` row.  A scoped cooldown therefore never touches
        the shared quota's cooldown state and vice versa.
        """
        target = pool_id if scope is None else _scope_pool_id(scope)
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            if scope is None:
                self._ensure_pool(conn, pool_id)
            else:
                self._ensure_scope_pool(conn, scope)
            now = self._db_now(conn)
            seconds = min(3600.0, max(1.0, float(cooldown_seconds)))
            until = now + timedelta(seconds=seconds)
            conn.execute(
                text("UPDATE model_admission_pools SET cooldown_until = GREATEST(cooldown_until, :until), "
                     "cooldown_retry_after = :ra, updated_at = :now WHERE pool_id = :pid"),
                {"pid": target, "until": until, "ra": int(min(3600, max(1, retry_after or seconds))), "now": now},
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

    def clear_probe_circuit(self, probe: Probe) -> bool:
        """Close a circuit by CAS, only while *this* probe is still valid.

        The update is guarded by two independent facts:

        * the probe's ``owner_token`` still has a live lease row, and
        * ``cooldown_until`` still equals the marker the probe observed.

        A late success from a request that started before the failure owns no
        probe and fails the first guard; an expired probe, or a newer failure
        that moved the marker, fails one of them.  Either way the newer fault
        survives, and only the caller that actually proved the channel healthy
        can reopen it.
        """
        if not probe.token:
            return False
        with self._engine.begin() as conn:
            self._set_timeout(conn)
            # Serialize with successor probes and failure updates before
            # checking ownership/expiry; a pre-lock snapshot can be stale.
            row = conn.execute(
                text("SELECT cooldown_until FROM model_admission_pools "
                     "WHERE pool_id = :pid FOR UPDATE"),
                {"pid": probe.pool_id},
            ).first()
            if row is None or row[0] != probe.expected_until:
                return False
            now = self._db_now(conn)
            held = conn.execute(
                text(
                    "SELECT 1 FROM model_admission_leases "
                    "WHERE pool_id = :pid AND owner_token = :token AND lease_until > :ts"
                ),
                {"pid": probe.pool_id, "token": probe.token, "ts": now},
            ).first()
            if held is None:
                return False
            result = conn.execute(
                text(
                    "UPDATE model_admission_pools SET cooldown_until = NULL, "
                    "cooldown_retry_after = NULL, updated_at = :now "
                    "WHERE pool_id = :pid AND cooldown_until = :expected"
                ),
                {"pid": probe.pool_id, "now": now, "expected": probe.expected_until},
            )
            return (result.rowcount or 0) > 0

    def scope_cooldown_remaining(self, pool_id: str, scope: str) -> float:
        if not is_failure_scope(scope):
            return 0.0
        with self._engine.connect() as conn:
            self._set_timeout(conn)
            row = conn.execute(
                text("SELECT cooldown_until FROM model_admission_pools WHERE pool_id = :pid"),
                {"pid": _scope_pool_id(scope)},
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
        # token -> (pool_id, kind, purpose, lease_until, failure_scope)
        self.leases: dict[str, tuple[str, str, str, datetime, str | None]] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.cooldown_retry_after: dict[str, float] = {}

    def _prune(self, now: datetime) -> list[tuple[str, str, str]]:
        live: list[tuple[str, str, str]] = []
        for token, (pid, k, purpose, until, _scope) in list(self.leases.items()):
            if until <= now:
                self.leases.pop(token, None)
            else:
                live.append((pid, k, purpose))
        return live

    def _claim_probe(self, circuit: str, now: datetime, lease_ttl_seconds: float
                     ) -> tuple[str, Probe]:
        """Mirror of the Postgres single-probe half-open semantics.

        ``cooldown_until`` is not advanced: a newer failure moves it to a future
        value, which is exactly what invalidates a late success's CAS.
        """
        until = self.cooldown_until.get(circuit)
        if until is None:
            return "closed", Probe(circuit, "", None)
        if until > now:
            return "cooling", Probe(circuit, "", None)
        busy = any(pid == circuit and k == "probe" and lease_until > now
                   for pid, k, _p, lease_until, _s in self.leases.values())
        if busy:
            return "cooling", Probe(circuit, "", None)
        token = uuid.uuid4().hex
        self.leases[token] = (
            circuit, "probe", "probe", now + timedelta(seconds=float(lease_ttl_seconds)), None,
        )
        return "probe", Probe(circuit, token, until)

    def _drop_probes(self, probes: list[Probe]) -> None:
        for probe in probes:
            if probe.token:
                self.leases.pop(probe.token, None)

    def acquire(self, pool_id, kind, purpose, *, lease_ttl_seconds, acquire_timeout_seconds,
                now=None, failure_scope=None):
        scope = failure_scope if is_failure_scope(failure_scope) else None
        deadline_mono = time.monotonic() + acquire_timeout_seconds
        while True:
            with self._lock:
                now = _now()
                probes: list[Probe] = []
                for circuit in [pool_id] + ([_scope_pool_id(scope)] if scope else []):
                    status, probe = self._claim_probe(circuit, now, lease_ttl_seconds)
                    if status == "cooling":
                        self._drop_probes(probes)
                        raise AdmissionCooldownError(
                            retry_after=self.cooldown_retry_after.get(circuit))
                    if status == "probe":
                        probes.append(probe)
                live = [entry for entry in self._prune(now)
                        if entry[0] == pool_id and entry[1] != "probe"]
                cap, bg_cap, ocr_cap = _default_capacity(pool_id)
                total = len(live)
                bgcount = sum(1 for _, k, _ in live if k == BACKGROUND)
                ocr_count = sum(1 for _, _, p in live if p == OCR)
                # Background work never consumes the last slot, so a foreground
                # call always keeps one free.  capacity 1 reserves nothing.
                bg_effective = min(bg_cap, max(0, cap - 1)) if cap > 1 else bg_cap
                if (total < cap
                        and not (kind == BACKGROUND and bgcount >= bg_effective)
                        and not (purpose == OCR and ocr_count >= ocr_cap)):
                    token = uuid.uuid4().hex
                    until = now + timedelta(seconds=lease_ttl_seconds)
                    self.leases[token] = (pool_id, kind, purpose, until, scope)
                    return Lease(pool_id=pool_id, owner_token=token, kind=kind,
                                 purpose=purpose, lease_until=until, failure_scope=scope,
                                 probes=tuple(probes))
                # Busy: give the probe back so the next caller can use it.
                self._drop_probes(probes)
            if time.monotonic() >= deadline_mono:
                raise AdmissionBusyError("模型额度繁忙")
            time.sleep(0.01)

    def release(self, lease: Lease) -> bool:
        if lease is None:
            return True
        with self._lock:
            held = self.leases.get(lease.owner_token)
            released = held is not None and held[0] == lease.pool_id
            if released:
                del self.leases[lease.owner_token]
            for probe in lease.probes:
                self.leases.pop(probe.token, None)
            return released

    def apply_cooldown(self, pool_id: str, cooldown_seconds: float,
                       retry_after: float | None = None) -> None:
        with self._lock:
            until = _now() + timedelta(seconds=min(3600, max(1, cooldown_seconds)))
            self.cooldown_until[pool_id] = max(self.cooldown_until.get(pool_id, until), until)
            self.cooldown_retry_after[pool_id] = float(retry_after) if retry_after else float(cooldown_seconds)

    def apply_scope_cooldown(self, pool_id: str, scope: str, cooldown_seconds: float,
                             retry_after: float | None = None) -> None:
        if not is_failure_scope(scope):
            raise ValueError("invalid failure scope")
        self.apply_cooldown(_scope_pool_id(scope), cooldown_seconds, retry_after)

    def clear_probe_circuit(self, probe: Probe) -> bool:
        """Mirror of the Postgres guarded CAS: token must be live *and* current."""
        if not probe.token:
            return False
        with self._lock:
            now = _now()
            held = self.leases.get(probe.token)
            if held is None or held[0] != probe.pool_id or held[3] <= now:
                return False
            if self.cooldown_until.get(probe.pool_id) != probe.expected_until:
                return False
            self.cooldown_until.pop(probe.pool_id, None)
            self.cooldown_retry_after.pop(probe.pool_id, None)
            return True

    def cooldown_remaining(self, pool_id: str) -> float:
        with self._lock:
            until = self.cooldown_until.get(pool_id)
            if until is None:
                return 0.0
            return max(0.0, (until - _now()).total_seconds())

    def scope_cooldown_remaining(self, pool_id: str, scope: str) -> float:
        if not is_failure_scope(scope):
            return 0.0
        return self.cooldown_remaining(_scope_pool_id(scope))

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
# A single shared pool means one upstream talks for the whole product, so the
# cooldown an upstream status implies depends on *what we actually know*:
#
#   429       The upstream itself is asking us to back off.  We cannot tell
#             which model is responsible, so this stays **global** and uses the
#             caller-supplied Retry-After (falling back to a conservative 60 s).
#   401/403   Almost always one revoked credential -> **scoped** when the call
#             site supplied a ``failure_scope``.
#   5xx       One model or endpoint being sick must not freeze every other
#             foreground ask -> **scoped** when a ``failure_scope`` was
#             supplied.
#
# Without a scope there is nothing safe to attribute the fault to, so a
# 401/403/5xx is recorded nowhere at all: a global 5xx cooldown used to take
# the whole product offline because one background model was flaky.  The caller
# still surfaces the real error through its own error path; these cooldowns only
# prevent hot-spamming.  Timeouts and unparseable JSON output are *not* channel
# faults and never set a shared cooldown.
#
# Every circuit is half-open: once its cooldown elapses exactly one caller may
# probe, and a success closes the circuit by CAS.

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


def classify_response(status_code: int, failure_scope: str | None = None) -> tuple[str, float] | None:
    """Map an upstream status to the cooldown it justifies.

    Returns ``None`` when the status must not set any cooldown.  The first
    element of the tuple is the routing target: ``"global"`` for the shared pool
    row, ``"scope"`` for this scope's own circuit.  The second is the seconds.
    """
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return None
    scoped = is_failure_scope(failure_scope)
    if status == 429:
        # Global: the upstream, not we, is asking for the back-off and we do
        # not know which model is responsible.
        return "global", _DEFAULT_RETRY_AFTER
    if status in (401, 403):
        return ("scope", _AUTH_COOLDOWN_SECONDS) if scoped else None
    if 500 <= status <= 599:
        return ("scope", _TRANSIENT_COOLDOWN_SECONDS) if scoped else None
    return None


def apply_response_cooldown(
    pool_id: str,
    status_code: int,
    retry_after: float | None = None,
    failure_scope: str | None = None,
) -> None:
    """Apply the cooldown an upstream HTTP status actually justifies.

    * ``429`` is always **global** and honours ``Retry-After`` (falling back to
      a conservative 60 s).
    * ``401/403`` and ``5xx`` are **scoped** when ``failure_scope`` was
      supplied, so one broken model or credential cannot freeze unrelated
      foreground work.  Without a scope they set no cooldown at all: there is
      nothing safe to attribute the fault to, and a global 5xx cooldown used to
      take the whole product offline because one background model was flaky.
    * ``2xx`` and other ``4xx`` set nothing.

    The current request's real failure is left untouched; callers must still
    surface it via their own error handling.  Timeouts and unparseable output
    are not channel faults and never reach this function.
    """
    routed = classify_response(status_code, failure_scope)
    if routed is None:
        return
    target, seconds = routed
    ra = _bounded_retry_after(retry_after)
    if target == "global":
        apply_cooldown(pool_id, ra or seconds, retry_after=ra)
        return
    try:
        admission_backend().apply_scope_cooldown(pool_id, failure_scope, seconds)
    except Exception:  # noqa: BLE001 - a coordination hiccup must not fail the call
        logger.warning("model_admission_scope_cooldown_failed scope=%s", failure_scope)



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
        *,
        pool_id: str = DEFAULT_POOL,
        failure_scope: str | None = None,
        probes: tuple[Probe, ...] = (),
    ) -> None:
        self._backend = backend
        self._deadline = deadline_monotonic
        self._lease_until = lease_until
        self._pool_id = pool_id
        self._scope = failure_scope if is_failure_scope(failure_scope) else None
        self._probes = tuple(probes)
        self._started = False
        self._circuit_closed = False

    @property
    def failure_scope(self) -> str | None:
        return self._scope

    def record_response(self, status_code: int, retry_after: float | None = None) -> None:
        """Report the upstream HTTP status and route the resulting cooldown.

        Call this after **every** HTTP response in the channel and before the
        channel is released, so each cooldown is attributed to this exact
        attempt while the half-open probe - if any - is still owned.  Call it
        more than once: the Ollama legacy fallback and the embedding canary each
        issue a second request inside one channel, and an error there is a real
        fault that must not be swallowed by an earlier ``200``.

        Routing:

        * ``429`` -> global cooldown.
        * ``401/403`` and ``5xx`` -> this scope's circuit only.
        * ``2xx`` -> close every circuit this attempt holds a live probe for,
          guarded by that probe's own ownership.  Closing happens **at most
          once** per channel: the first success is the proof; a later one has
          nothing left to prove.
        * a transport timeout or unparseable body never reaches here and so
          never sets a shared cooldown.

        This is synchronous and never raises: a coordination hiccup must not
        turn a successful model answer into an error.
        """
        try:
            status = int(status_code)
        except (TypeError, ValueError):
            return
        try:
            if 200 <= status < 300:
                if self._circuit_closed:
                    return  # already proved healthy; nothing left to close
                self._circuit_closed = True
                for probe in self._probes:
                    self._backend.clear_probe_circuit(probe)
                return
            apply_response_cooldown(
                self._pool_id, status, retry_after, self._scope,
            )
        except Exception:  # noqa: BLE001 - never fail the caller over bookkeeping
            logger.warning("model_admission_record_response_failed pool=%s", self._pool_id)

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


def _acquire_with_scope(backend: AdmissionBackend, pid: str, kind: str, purpose: str,
                        ttl: float, scope: str | None) -> Lease:
    """Thread-side acquire that passes the optional failure scope through."""
    return backend.acquire(pid, kind, purpose, lease_ttl_seconds=ttl,
                           acquire_timeout_seconds=_config().acquire_timeout,
                           failure_scope=scope)


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
    failure_scope: str | None = None,
) -> AsyncIterator["_AsyncChannel"]:
    """Acquire a slot (before any request), yield a channel whose ``run``
    enforces ``asyncio.wait_for`` hard deadline (cancellable), and release
    **only in the ``finally`` after the transport body exits** so the slot is
    held for the entire request.  The threaded acquire is shielded; a
    cancellation during a still-running acquire waits for its bounded result
    and releases any acquired lease before propagating cancellation.

    ``failure_scope`` is optional.  When supplied (via :func:`failure_scope`)
    this request also passes through that channel scope's circuit, so a 5xx
    from this model/credential cools only this scope, while other models and
    the shared quota keep serving.  A scope **still acquires the same shared
    slot**: it adds isolation, it never adds capacity.
    """
    hard, ttl = _hard_and_ttl(hard_lifetime_seconds)
    pid = pool_id or channel_pool_id(purpose)
    scope = failure_scope if is_failure_scope(failure_scope) else None
    backend = admission_backend()
    # Absolute monotonic deadline captured BEFORE the acquire: the run window
    # starts here, so a delayed acquire can never push the request past the
    # lease's ``lease_until`` (ttl == hard + padding).
    deadline = time.monotonic() + hard
    acquire_fut = asyncio.ensure_future(
        asyncio.to_thread(
            _acquire_with_scope, backend, pid, kind or current_kind(), purpose,
            ttl, scope,
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
            # Releases the real slot *and* any probe this late acquire claimed,
            # leaving an elapsed circuit ready for the next request to probe.
            await _release(backend, late_lease)
        raise
    if isinstance(lease, AdmissionError):
        raise lease
    try:
        yield _AsyncChannel(
            backend=backend, deadline_monotonic=deadline, lease_until=lease.lease_until,
            pool_id=pid, failure_scope=lease.failure_scope, probes=lease.probes,
        )
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
