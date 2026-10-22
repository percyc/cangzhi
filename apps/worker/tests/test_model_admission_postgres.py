"""Explicit isolated migrated PostgreSQL multiprocess tests (ADR-034).

These never touch the runtime DB: they require
``CANGZHI_MODEL_ADMISSION_TEST_DATABASE_URL`` (asserted to end with
``/cangzhi_test``) and prove the cross-process cooperative quota by running
independent processes against the same Postgres.
"""
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text

from apps.api.core.db import Base
from apps.api.models.model_admission import ModelAdmissionLease, ModelAdmissionPool
from apps.api.services.model_admission import (
    AdmissionBusyError,
    AdmissionCooldownError,
    AdmissionUnavailableError,
    DEFAULT_POOL,
    PostgresAdmissionBackend,
    FOREGROUND,
    BACKGROUND,
    OCR,
    CHAT,
    EMBEDDING,
)


def _url():
    url = os.getenv("CANGZHI_MODEL_ADMISSION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("explicit isolated database required")
    assert url.endswith("/cangzhi_test")
    return url


def _engine(url):
    engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine, tables=[ModelAdmissionPool.__table__, ModelAdmissionLease.__table__])
    return engine


def _reset(engine, pool_id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM model_admission_leases WHERE pool_id = :pid"), {"pid": pool_id})
        conn.execute(text("DELETE FROM model_admission_pools WHERE pool_id = :pid"), {"pid": pool_id})


def _mp_acquire(url, pool_id, kind, purpose, ttl, timeout):
    """Top-level helper for ProcessPoolExecutor: returns a serialisable tuple."""
    backend = PostgresAdmissionBackend(url)
    try:
        lease = backend.acquire(pool_id, kind, purpose, lease_ttl_seconds=ttl,
                                acquire_timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001 - encode failure to compare counts
        return ("error", type(exc).__name__)
    return ("ok", lease.owner_token)


def _acquire(engine, pool_id, kind=FOREGROUND, purpose=CHAT, ttl=90.0, timeout=1.0):
    backend = PostgresAdmissionBackend(engine=engine)
    return backend.acquire(pool_id, kind, purpose, lease_ttl_seconds=ttl,
                           acquire_timeout_seconds=timeout)


def test_pg_multiprocess_capacity_single_shared_pool():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL  # capacity 2, background 1
    try:
        _reset(engine, pool_id)
        attempts = 5
        with ProcessPoolExecutor(max_workers=attempts) as pool:
            results = [
                pool.submit(_mp_acquire, url, pool_id, FOREGROUND, CHAT, 120, 1.0)
                for _ in range(attempts)
            ]
            outcomes = [f.result() for f in results]
        ok = [o for o in outcomes if o[0] == "ok"]
        busy = [o for o in outcomes if o[0] == "error" and o[1] == "AdmissionBusyError"]
        assert len(ok) == 2, outcomes   # at most the shared capacity
        assert len(busy) == attempts - 2
        assert len(set(token for _, token in ok)) == 2  # unique owners
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_foreground_background_and_ocr_sublimit():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL
    try:
        _reset(engine, pool_id)
        fg = _acquire(engine, pool_id, FOREGROUND, CHAT)
        bg = _acquire(engine, pool_id, BACKGROUND, EMBEDDING)
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, BACKGROUND, CHAT, timeout=0.1)  # background cap 1
        # total 2 reached; foreground also blocked now
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, FOREGROUND, CHAT, timeout=0.1)
        for lease in (fg, bg):
            _acquire_release(engine, lease)

        # OCR sub-limit: 1 OCR ok, second OCR busy while chat slot remains.
        _reset(engine, pool_id)
        ocr1 = _acquire(engine, pool_id, FOREGROUND, OCR)
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, FOREGROUND, OCR, timeout=0.1)
        chat = _acquire(engine, pool_id, FOREGROUND, CHAT)  # pool still serves chat
        _acquire_release(engine, ocr1)
        _acquire_release(engine, chat)
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def _acquire_release(engine, lease):
    backend = PostgresAdmissionBackend(engine=engine)
    assert backend.release(lease) is True


def test_pg_owner_cas_release_and_release_frees_slot():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL
    backend = PostgresAdmissionBackend(engine=engine)
    try:
        _reset(engine, pool_id)
        lease = _acquire(engine, pool_id)
        second = _acquire(engine, pool_id)
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, timeout=0.1)  # full
        assert backend.release(lease) is True
        assert backend.release(lease) is False  # CAS: second release is a no-op
        assert backend.release(second) is True
        _acquire(engine, pool_id)  # slot is free again
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM model_admission_leases WHERE pool_id = :pid"), {"pid": pool_id})
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_typed_cooldown_with_retry_after():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL
    backend = PostgresAdmissionBackend(engine=engine)
    try:
        _reset(engine, pool_id)
        backend.apply_cooldown(pool_id, cooldown_seconds=600, retry_after=11)
        with pytest.raises(AdmissionCooldownError) as exc_info:
            _acquire(engine, pool_id, timeout=0.2)
        assert exc_info.value.retry_after == 11
        assert backend.cooldown_remaining(pool_id) > 0
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_busy_deadline_typed():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL
    try:
        _reset(engine, pool_id)
        holder = _acquire(engine, pool_id, ttl=900)
        second = _acquire(engine, pool_id, ttl=900)
        started = time.monotonic()
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, timeout=0.3)
        assert time.monotonic() - started < 2.0
        _acquire_release(engine, holder)
        _acquire_release(engine, second)
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_unavailable_fail_closed():
    # A completely wrong host must fail closed, not fail open.
    backend = PostgresAdmissionBackend(url="postgresql://nope:nope@127.0.0.1:1/nope")
    with pytest.raises(AdmissionUnavailableError):
        backend.acquire(DEFAULT_POOL, FOREGROUND, CHAT,
                        lease_ttl_seconds=90, acquire_timeout_seconds=0.5)


def test_pg_multiprocess_foreground_background_via_threads():
    url = _url()
    engine = _engine(url)
    pool_id = DEFAULT_POOL
    try:
        _reset(engine, pool_id)
        # 1 background + 1 foreground may both hold slots; a second background
        # must fail even via concurrent acquisition.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(_mp_acquire, url, pool_id, BACKGROUND, CHAT, 120, 1.0),
                pool.submit(_mp_acquire, url, pool_id, FOREGROUND, EMBEDDING, 120, 1.0),
                pool.submit(_mp_acquire, url, pool_id, BACKGROUND, CHAT, 120, 1.0),
            ]
            outcomes = [f.result() for f in futures]
        assert sum(1 for o in outcomes if o[0] == "ok") == 2
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_expiry_and_late_release_cannot_remove_successor():
    engine = _engine(_url())
    backend = PostgresAdmissionBackend(engine=engine)
    pool_id = "expiry-regression"
    try:
        _reset(engine, pool_id)
        old = _acquire(engine, pool_id, kind=BACKGROUND, ttl=.1)
        time.sleep(.15)
        new = _acquire(engine, pool_id, kind=BACKGROUND)
        assert new.owner_token != old.owner_token
        assert backend.release(old) is False
        with pytest.raises(AdmissionBusyError):
            _acquire(engine, pool_id, kind=BACKGROUND, timeout=.05)
        assert backend.release(new)
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_cooldown_first_use_and_concurrent_shortening_guard():
    engine = _engine(_url())
    backend = PostgresAdmissionBackend(engine=engine)
    pool_id = "cooldown-regression"
    try:
        _reset(engine, pool_id)
        backend.apply_cooldown(pool_id, 120)
        backend.apply_cooldown(pool_id, 1)
        assert backend.cooldown_remaining(pool_id) > 110
        with pytest.raises(AdmissionCooldownError):
            _acquire(engine, pool_id)
    finally:
        _reset(engine, pool_id)
        engine.dispose()


def test_pg_pool_lock_fails_closed_with_bounded_wait():
    engine = _engine(_url())
    backend = PostgresAdmissionBackend(engine=engine)
    pool_id = "lock-regression"
    try:
        _reset(engine, pool_id)
        lease = _acquire(engine, pool_id)
        backend.release(lease)
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(text("SELECT id FROM model_admission_pools WHERE pool_id=:pid FOR UPDATE"),
                           {"pid": pool_id})
            started = time.monotonic()
            with pytest.raises(AdmissionUnavailableError):
                _acquire(engine, pool_id, timeout=.1)
            assert time.monotonic() - started < 3
            transaction.rollback()
        recovered = _acquire(engine, pool_id)
        assert backend.release(recovered)
    finally:
        _reset(engine, pool_id)
        engine.dispose()
