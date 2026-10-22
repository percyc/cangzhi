"""Unit tests for cross-process model-call admission (ADR-034).

These use the test-only in-memory backend (no DB) and cover the core
quantum semantics: single conservative shared pool (no per-purpose limit
multiplication), foreground/background capacity, OCR sub-limit, typed
cooldown / busy / unavailable, owner CAS release, no-request-sent-before-busy,
and the async channel's finite hard deadline.
"""
import asyncio
import time
from datetime import timedelta

import pytest

from apps.api.services import model_admission as ma
from apps.api.services.model_admission import (
    AdmissionBusyError,
    AdmissionCooldownError,
    AdmissionError,
    AdmissionUnavailableError,
    InMemoryAdmissionBackend,
    BACKGROUND,
    FOREGROUND,
    OCR,
    CHAT,
    EMBEDDING,
)


@pytest.fixture(autouse=True)
def _fresh_backend():
    backend = InMemoryAdmissionBackend()
    ma.configure_admission_backend(backend)
    yield backend
    ma.reset_admission_backend()


def _acquire(backend, pool_id, kind=FOREGROUND, purpose=CHAT, ttl=90.0, timeout=0.2):
    return backend.acquire(
        pool_id, kind, purpose, lease_ttl_seconds=ttl, acquire_timeout_seconds=timeout
    )


def test_single_default_pool_does_not_multiply_limits():
    backend = ma.admission_backend()
    # chat foreground x2 fills the single default pool (capacity 2).
    l1 = _acquire(backend, ma.DEFAULT_POOL, purpose=CHAT)
    l2 = _acquire(backend, ma.DEFAULT_POOL, purpose=CHAT)
    # embedding shares the SAME pool by default -> must be busy, not +2.
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, purpose=EMBEDDING, timeout=0.05)
    backend.release(l1)
    backend.release(l2)


def test_foreground_background_capacity():
    backend = ma.admission_backend()
    # total 2, background 1. Start with a background: it leaves a foreground slot.
    bg = _acquire(backend, ma.DEFAULT_POOL, kind=BACKGROUND)
    # second background blocked by background_capacity even though total 2 allows it.
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, kind=BACKGROUND, timeout=0.05)
    # a foreground can still take the remaining total slot.
    fg = _acquire(backend, ma.DEFAULT_POOL, kind=FOREGROUND)
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, kind=FOREGROUND, timeout=0.05)
    backend.release(bg)
    backend.release(fg)


def test_ocr_sublimit_within_shared_pool():
    backend = ma.admission_backend()
    ocr1 = _acquire(backend, ma.DEFAULT_POOL, purpose=OCR)
    # OCR sub-limit 1: second OCR busy even though chat slots remain.
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, purpose=OCR, timeout=0.05)
    # chat can still use the pool while OCR holds one slot.
    chat = _acquire(backend, ma.DEFAULT_POOL, purpose=CHAT)
    backend.release(ocr1)
    backend.release(chat)


def test_owner_cas_release():
    backend = ma.admission_backend()
    lease1 = _acquire(backend, ma.DEFAULT_POOL)
    lease2 = _acquire(backend, ma.DEFAULT_POOL)  # fills capacity (2)
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, timeout=0.05)
    assert backend.release(lease1) is True
    # releasing again (or a foreign token) is a no-op -> False.
    assert backend.release(lease1) is False
    # after release the slot is free again
    _acquire(backend, ma.DEFAULT_POOL)
    backend.release(lease2)


def test_typed_cooldown_with_retry_after_no_retry():
    backend = ma.admission_backend()
    backend.apply_cooldown(ma.DEFAULT_POOL, cooldown_seconds=30, retry_after=7)
    started = time.monotonic()
    with pytest.raises(AdmissionCooldownError) as exc_info:
        _acquire(backend, ma.DEFAULT_POOL, timeout=0.2)
    # Typed cooldown with the retry-after surfaced, raised immediately (no
    # repeated internal retries / bounded-wait loops masking it).
    assert exc_info.value.retry_after == 7
    assert time.monotonic() - started < 0.2


@pytest.mark.parametrize("raw,expected", [("NaN", None), ("Infinity", None), ("-1", None),
    ("0", None), ("45", 45), ("999999", 3600), ("bad", None)])
def test_retry_after_is_finite_and_bounded(raw, expected):
    assert ma.parse_retry_after({"Retry-After": raw}) == expected


def test_retry_after_http_date():
    from email.utils import format_datetime
    value = ma.parse_retry_after({"Retry-After": format_datetime(ma._now() + timedelta(seconds=90))})
    assert 88 <= value <= 90


@pytest.mark.parametrize("status,seconds", [(429, 60), (401, 300), (403, 300), (503, 60), (404, 0)])
def test_response_cooldown_categories(status, seconds):
    ma.apply_response_cooldown(ma.DEFAULT_POOL, status)
    remaining = ma.admission_backend().cooldown_remaining(ma.DEFAULT_POOL)
    assert max(0, seconds - 2) <= remaining <= seconds


def test_typed_busy_quickly_bounded():
    backend = ma.admission_backend()
    l1 = _acquire(backend, ma.DEFAULT_POOL, ttl=900)
    l2 = _acquire(backend, ma.DEFAULT_POOL, ttl=900)  # fill capacity (2)
    started = time.monotonic()
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ma.DEFAULT_POOL, timeout=0.2)
    assert time.monotonic() - started < 1.0
    backend.release(l1)
    backend.release(l2)


def test_role_background_classifies_calls():
    ma.set_process_role("worker")
    try:
        assert ma.current_kind() == BACKGROUND
        backend = ma.admission_backend()

        async def body():
            async with ma.async_channel(CHAT, hard_lifetime_seconds=5) as channel:
                with backend._lock:
                    entry = next(iter(backend.leases.values()))
                    assert entry[1] == BACKGROUND  # classified from the role

        asyncio.run(body())
    finally:
        ma.set_process_role(FOREGROUND)


def test_unavailable_fail_closed():
    # A completely wrong host must fail closed, not fail open.
    backend = ma.PostgresAdmissionBackend(url="postgresql://nope:nope@127.0.0.1:1/nope")
    with pytest.raises(AdmissionUnavailableError):
        backend.acquire(ma.DEFAULT_POOL, FOREGROUND, CHAT,
                        lease_ttl_seconds=90, acquire_timeout_seconds=0.5)


def test_no_request_sent_when_busy():
    """A busy acquire must leave any outbound call un-invoked."""
    backend = ma.admission_backend()
    holder1 = _acquire(backend, ma.DEFAULT_POOL, ttl=900)
    holder2 = _acquire(backend, ma.DEFAULT_POOL, ttl=900)  # fill capacity (2)
    sent = {"count": 0}

    async def run_when_busy():
        try:
            async with ma.async_channel(CHAT, hard_lifetime_seconds=5) as channel:
                async def fake_request():
                    sent["count"] += 1
                    return "ok"

                return await channel.run(fake_request())
        except AdmissionError:
            return None

    result = asyncio.run(run_when_busy())
    assert result is None          # busy -> admission error
    assert sent["count"] == 0      # no HTTP request was attempted
    backend.release(holder1)
    backend.release(holder2)


def test_async_channel_hard_deadline_and_release():
    """channel.run enforces a finite hard deadline; the slot is released on
    exit so a later acquire succeeds."""
    backend = ma.admission_backend()

    async def work():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=1.0) as channel:
            async def slow():
                await asyncio.sleep(2.0)
                return "x"

            from asyncio import TimeoutError as AsyncTimeout
            try:
                await channel.run(slow())
            except AsyncTimeout:
                return "timed_out"
            finally:
                pass
        # after the channel exits, the slot is released
        lease = _acquire(backend, ma.DEFAULT_POOL, purpose=CHAT)
        backend.release(lease)

    assert asyncio.run(work()) == "timed_out"


def test_run_sync_facade_releases_inside_transport():
    """run_sync runs an async transport (with its admission channel) to
    completion in a fresh loop and releases the slot inside the transport."""
    backend = ma.admission_backend()

    async def transport():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5) as channel:
            async def fake():
                return "called"

            return await channel.run(fake())

    assert ma.run_sync(transport) == "called"
    # Slot was returned: a fresh acquire succeeds.
    lease = _acquire(backend, ma.DEFAULT_POOL)
    backend.release(lease)


def test_slot_held_inside_channel_until_context_exit():
    """The slot must be held for the entire request: with one external holder
    and the channel taking the last slot, a further acquire is blocked while we
    are inside the channel, and freed only after exit."""
    backend = ma.admission_backend()
    holder = _acquire(backend, ma.DEFAULT_POOL, ttl=900)  # capacity 2 -> 1 left
    seen_busy = {"value": False}

    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5) as channel:
            async def fake_request():
                try:
                    _acquire(backend, ma.DEFAULT_POOL, timeout=0.05)
                except AdmissionBusyError:
                    seen_busy["value"] = True
                await asyncio.sleep(0.02)
                return "done"

            return await channel.run(fake_request())

    assert asyncio.run(body()) == "done"
    backend.release(holder)
    # After the channel context exits the slot is free again.
    lease = _acquire(backend, ma.DEFAULT_POOL)
    backend.release(lease)
    assert seen_busy["value"] is True


def _test_cancel_during_slow_acquire_releases_slot():
    """If the caller is cancelled while the (threaded) acquire is still in
    flight, the lease that the orphaned acquire later obtains must be released,
    so the pool ends empty (no leaked slot)."""
    base = ma.InMemoryAdmissionBackend()

    class SlowAcquire(ma.InMemoryAdmissionBackend):
        def acquire(self, *a, **kw):
            time.sleep(0.3)
            return super().acquire(*a, **kw)

    backend = SlowAcquire()
    ma.configure_admission_backend(backend)
    cancelled = {"value": False}

    async def body():
        try:
            async with ma.async_channel(CHAT, hard_lifetime_seconds=5):
                pass
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise

    loop = asyncio.new_event_loop()
    task = loop.create_task(body())
    loop.call_later(0.05, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    # Keep the loop alive long enough for the orphaned acquire thread to
    # finish and the done-callback to release the late lease.
    loop.run_until_complete(asyncio.sleep(0.6))
    loop.close()

    assert cancelled["value"] is True
    with ma.admission_backend()._lock:
        assert ma.admission_backend().leases == {}  # no leaked slot
    ma.reset_admission_backend()
