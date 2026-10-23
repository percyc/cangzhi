"""Unit tests for scoped failure isolation in shared model admission.

Covers the routing policy (429 global, 401/403/5xx scoped, unscoped 5xx sets
nothing), scope opacity, half-open single-probe recovery, the late-success CAS,
background capacity clamping, and probe/lease cleanup on cancellation.

Runs on the test-only in-memory backend; the PostgreSQL equivalents live in
``apps/worker/tests/test_model_admission_isolation_postgres.py``.
"""
import asyncio
import threading
import time
from datetime import timedelta

import pytest

from apps.api.services import model_admission as ma
from apps.api.services.model_admission import (
    AdmissionBusyError,
    AdmissionCooldownError,
    InMemoryAdmissionBackend,
    BACKGROUND,
    FOREGROUND,
    CHAT,
    EMBEDDING,
    OCR,
)

URL_A = "https://a.invalid/v1/chat/completions"
URL_B = "https://b.invalid/v1/chat/completions"


@pytest.fixture(autouse=True)
def _fresh_backend():
    backend = InMemoryAdmissionBackend()
    ma.configure_admission_backend(backend)
    yield backend
    ma.reset_admission_backend()


def _acquire(backend, pool_id=ma.DEFAULT_POOL, kind=FOREGROUND, purpose=CHAT,
             ttl=90.0, timeout=0.2, scope=None):
    return backend.acquire(pool_id, kind, purpose, lease_ttl_seconds=ttl,
                           acquire_timeout_seconds=timeout, failure_scope=scope)


def _scope(url=URL_A, model="m1", key="sk-one", purpose=CHAT):
    return ma.failure_scope(purpose, url, model, key)


# ---------------------------------------------------------------------------
# Scope opacity and stability
# ---------------------------------------------------------------------------


def test_scope_is_opaque_hex_and_stable():
    scope = _scope()
    assert ma.is_failure_scope(scope)
    assert len(scope) == 56
    # No raw material survives into the digest.
    assert URL_A not in scope and "sk-one" not in scope and "m1" not in scope
    assert scope == _scope()  # deterministic across calls/processes


def test_scope_pool_id_fits_pool_id_column():
    """``scope:`` + digest must stay inside VARCHAR(64)."""
    pid = ma._scope_pool_id(_scope())
    assert pid.startswith(ma.SCOPE_POOL_PREFIX)
    assert len(pid) <= 64


@pytest.mark.parametrize("field", ["purpose", "url", "model", "key"])
def test_every_component_changes_the_scope(field):
    base = dict(purpose=CHAT, url=URL_A, model="m1", key="sk-one")
    other = dict(base, **{field: "different"})
    assert ma.failure_scope(other["purpose"], other["url"], other["model"],
                            other["key"]) != ma.failure_scope(
        base["purpose"], base["url"], base["model"], base["key"])


def test_model_and_credential_are_case_sensitive():
    assert _scope(model="ModelA", key="sk-AbC") != _scope(model="modela", key="sk-AbC")
    assert _scope(model="m", key="sk-AbC") != _scope(model="m", key="sk-abc")


def test_component_delimiter_cannot_be_shifted_between_fields():
    """Canonical JSON encoding: no cross-field collision via separators."""
    assert ma.failure_scope(CHAT, "a", "bc", "") != ma.failure_scope(CHAT, "ab", "c", "")


@pytest.mark.parametrize("bad", ["", "zz", "A" * 56, "a" * 55, "a" * 57, None, 5])
def test_invalid_scopes_are_rejected(bad):
    assert not ma.is_failure_scope(bad)


def test_scope_row_is_isolated_from_the_real_pool_id():
    assert ma._scope_pool_id(_scope()) != ma.DEFAULT_POOL


def test_scope_namespace_cannot_be_configured_as_a_quota_pool(monkeypatch):
    monkeypatch.setattr(ma.settings, "model_admission_pool_map", '{"chat":"scope:reserved"}')
    assert ma.channel_pool_id(CHAT) == ma.DEFAULT_POOL


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------


def test_429_is_global_and_conservative():
    assert ma.classify_response(429) == ("global", 60.0)


def test_401_403_and_5xx_are_scoped_only_with_a_scope():
    scope = _scope()
    assert ma.classify_response(401, scope) == ("scope", 300.0)
    assert ma.classify_response(403, scope) == ("scope", 300.0)
    assert ma.classify_response(503, scope) == ("scope", 60.0)
    # Without a scope there is nothing safe to attribute the fault to.
    assert ma.classify_response(401) is None
    assert ma.classify_response(503) is None


@pytest.mark.parametrize("status", [200, 201, 400, 404, 422])
def test_healthy_and_client_errors_set_no_cooldown(status):
    assert ma.classify_response(status, _scope()) is None


def test_unscoped_5xx_does_not_globally_poison_the_pool():
    """Documented policy change: only 429 may cool the shared pool."""
    backend = ma.admission_backend()
    ma.apply_response_cooldown(ma.DEFAULT_POOL, 503)
    assert backend.cooldown_remaining(ma.DEFAULT_POOL) == 0.0
    # And the pool is still immediately usable.
    backend.release(_acquire(backend))


def test_scoped_5xx_leaves_the_shared_pool_usable():
    backend = ma.admission_backend()
    ma.apply_response_cooldown(ma.DEFAULT_POOL, 503, failure_scope=_scope())
    assert backend.cooldown_remaining(ma.DEFAULT_POOL) == 0.0
    # The failing scope is cooling though.
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, _scope()) > 0


def test_429_cools_the_pool_globally_even_with_a_scope():
    backend = ma.admission_backend()
    ma.apply_response_cooldown(ma.DEFAULT_POOL, 429, retry_after=7, failure_scope=_scope())
    assert 5 <= backend.cooldown_remaining(ma.DEFAULT_POOL) <= 7
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, _scope()) == 0.0


def test_scoped_cooldown_does_not_leak_to_other_scopes_or_purposes():
    backend = ma.admission_backend()
    broken = _scope()
    ma.apply_response_cooldown(ma.DEFAULT_POOL, 503, failure_scope=broken)
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, _scope(url=URL_B)) == 0.0
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, _scope(key="sk-two")) == 0.0
    assert backend.scope_cooldown_remaining(
        ma.DEFAULT_POOL, _scope(purpose=EMBEDDING)) == 0.0
    # An embedding on the same URL/key is unaffected.
    backend.release(_acquire(backend, purpose=EMBEDDING, scope=_scope(purpose=EMBEDDING)))
    backend.release(_acquire(backend, purpose=OCR, scope=_scope(purpose=OCR)))


def test_scoped_cooldown_never_shortens_a_longer_live_cooldown():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 300)
    first = backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope)
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) <= first


def test_scoped_cooldown_requires_a_valid_scope():
    with pytest.raises(ValueError):
        ma.admission_backend().apply_scope_cooldown(ma.DEFAULT_POOL, "not-a-scope", 60)


# ---------------------------------------------------------------------------
# The failing scope is blocked; healthy work is not
# ---------------------------------------------------------------------------


def test_cooling_scope_is_blocked_but_other_models_still_serve():
    backend = ma.admission_backend()
    broken = _scope(model="broken")
    healthy = _scope(model="healthy")
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, broken, 60)
    with pytest.raises(AdmissionCooldownError):
        _acquire(backend, scope=broken, timeout=0.05)
    backend.release(_acquire(backend, scope=healthy))


def test_a_background_5xx_does_not_block_foreground_or_embedding():
    """The regression this policy exists for."""
    backend = ma.admission_backend()
    broken = _scope(purpose=OCR, model="ocr-broken")
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, broken, 60)
    backend.release(_acquire(backend, kind=FOREGROUND, purpose=CHAT, scope=_scope()))
    backend.release(_acquire(backend, purpose=EMBEDDING, scope=_scope(purpose=EMBEDDING)))


# ---------------------------------------------------------------------------
# Half-open: exactly one probe, and the CAS
# ---------------------------------------------------------------------------


def _expire(backend, key):
    with backend._lock:
        backend.cooldown_until[key] = ma._now() - timedelta(seconds=1)


def test_expired_scope_allows_exactly_one_probe():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    probe = _acquire(backend, scope=scope)
    assert probe.probes, "an elapsed circuit must hand out a probe token"
    # Everyone else is still cooling while the probe is in flight.
    with pytest.raises(AdmissionCooldownError):
        _acquire(backend, scope=scope, timeout=0.05)
    backend.release(probe)


def test_probe_success_closes_the_circuit():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    probe = _acquire(backend, scope=scope)
    for held in probe.probes:
        assert backend.clear_probe_circuit(held) is True
    backend.release(probe)
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) == 0.0
    backend.release(_acquire(backend, scope=scope))


def test_global_429_probe_success_closes_the_global_circuit():
    backend = ma.admission_backend()
    backend.apply_cooldown(ma.DEFAULT_POOL, 60)
    _expire(backend, ma.DEFAULT_POOL)
    probe = _acquire(backend)
    for held in probe.probes:
        assert backend.clear_probe_circuit(held) is True
    backend.release(probe)
    assert backend.cooldown_remaining(ma.DEFAULT_POOL) == 0.0
    backend.release(_acquire(backend))


def test_probe_failure_extends_the_circuit():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    probe = _acquire(backend, scope=scope)
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 30)
    backend.release(probe)
    # Still cooling: a failed probe must not reopen traffic.
    with pytest.raises(AdmissionCooldownError):
        _acquire(backend, scope=scope, timeout=0.05)


def test_late_success_cannot_clear_a_newer_failure():
    """A request in flight before the failure must not vouch for the channel."""
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    old = _acquire(backend, scope=scope)
    old_probe = next(p for p in old.probes if p.pool_id == ma._scope_pool_id(scope))
    # A newer failure moves the marker to the future while `old` is in flight.
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 300)
    assert backend.clear_probe_circuit(old_probe) is False
    backend.release(old)
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) > 0
    with pytest.raises(AdmissionCooldownError):
        _acquire(backend, scope=scope, timeout=0.05)


def test_an_expired_probe_cannot_clear_the_newer_circuit():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    stale = _acquire(backend, scope=scope, ttl=0.01)
    stale_probe = next(p for p in stale.probes if p.pool_id == ma._scope_pool_id(scope))
    time.sleep(0.05)
    # A newer generation is now cooling; the expired probe must not clear it.
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 300)
    assert backend.clear_probe_circuit(stale_probe) is False
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) > 0


def test_clear_without_a_live_probe_is_a_no_op():
    backend = ma.admission_backend()
    assert backend.clear_probe_circuit(ma.Probe(ma.DEFAULT_POOL, "", None)) is False


def test_global_429_half_opens_with_one_probe():
    backend = ma.admission_backend()
    backend.apply_cooldown(ma.DEFAULT_POOL, 60)
    _expire(backend, ma.DEFAULT_POOL)
    probe = _acquire(backend)
    with pytest.raises(AdmissionCooldownError):
        _acquire(backend, timeout=0.05)
    backend.release(probe)


def test_a_probe_does_not_consume_shared_capacity():
    """Capacity 1 must still be able to half-open recover."""
    backend = ma.admission_backend()
    original = ma._default_capacity
    ma._default_capacity = lambda pool_id: (1, 1, 1)
    try:
        backend.apply_cooldown(ma.DEFAULT_POOL, 60)
        _expire(backend, ma.DEFAULT_POOL)
        probe = _acquire(backend, scope=_scope())
        # The probe lease lives on the circuit row, not in the pool's quota.
        with backend._lock:
            pool_leases = [entry for entry in backend.leases.values()
                           if entry[0] == ma.DEFAULT_POOL and entry[1] != "probe"]
            probe_leases = [entry for entry in backend.leases.values()
                            if entry[1] == "probe"]
        assert len(pool_leases) == 1, "only the real slot counts"
        assert probe_leases, "the probe is reserved on its circuit row"
        backend.release(probe)
    finally:
        ma._default_capacity = original


def test_busy_quota_hands_the_probe_back():
    """A busy pool must not burn the single half-open probe."""
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    filler = [_acquire(backend, kind=FOREGROUND, ttl=900) for _ in range(2)]
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, ttl=900, timeout=0.05)
    for lease in filler:
        backend.release(lease)
    # The probe is still available to the next caller.
    probe = _acquire(backend, scope=scope)
    assert probe.probes
    backend.release(probe)


# ---------------------------------------------------------------------------
# Background capacity clamping
# ---------------------------------------------------------------------------


def test_background_cannot_consume_the_last_slot_when_capacity_greater_than_one():
    backend = ma.admission_backend()
    cap, bg_cap, _ocr = ma._default_capacity(ma.DEFAULT_POOL)
    assert cap > 1, "this regression is about the multi-slot configuration"
    held = [_acquire(backend, kind=BACKGROUND, ttl=900) for _ in range(bg_cap)]
    # Background may not exceed its effective (capacity-1) ceiling.
    effective = min(bg_cap, cap - 1)
    assert len(held) == effective
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, kind=BACKGROUND, ttl=900, timeout=0.05)
    # The remaining slot is still available for interactive foreground work.
    foreground = _acquire(backend, kind=FOREGROUND, ttl=900)
    backend.release(foreground)
    for lease in held:
        backend.release(lease)


def test_capacity_one_still_allows_background_work():
    """With one slot there is nothing to reserve; documented, not preemption."""
    backend = ma.admission_backend()
    original = ma._default_capacity
    ma._default_capacity = lambda pool_id: (1, 1, 1)
    try:
        bg = _acquire(backend, kind=BACKGROUND, ttl=900)
        with pytest.raises(AdmissionBusyError):
            _acquire(backend, kind=BACKGROUND, ttl=900, timeout=0.05)
        backend.release(bg)
    finally:
        ma._default_capacity = original


def test_scopes_still_consume_the_shared_quota():
    """A scope adds isolation, never extra capacity."""
    backend = ma.admission_backend()
    a = _acquire(backend, scope=_scope(model="a"))
    b = _acquire(backend, scope=_scope(model="b"))
    with pytest.raises(AdmissionBusyError):
        _acquire(backend, scope=_scope(model="c"), timeout=0.05)
    backend.release(a)
    backend.release(b)


# ---------------------------------------------------------------------------
# Channel integration: record_response
# ---------------------------------------------------------------------------


def _respond(status, scope=None, retry_after=None):
    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope) as channel:
            channel.record_response(status, retry_after)
            return channel

    return asyncio.run(body())


def test_record_response_scopes_a_5xx():
    backend = ma.admission_backend()
    scope = _scope()
    _respond(503, scope=scope)
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) > 0
    assert backend.cooldown_remaining(ma.DEFAULT_POOL) == 0.0


def test_record_response_429_is_global():
    backend = ma.admission_backend()
    _respond(429, scope=_scope(), retry_after=9)
    assert 5 <= backend.cooldown_remaining(ma.DEFAULT_POOL) <= 9


def test_record_response_2xx_on_a_healthy_circuit_is_a_no_op():
    backend = ma.admission_backend()
    _respond(200, scope=_scope())
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, _scope()) == 0.0


def test_record_response_never_raises_on_a_bad_status():
    _respond("not-a-status", scope=_scope())
    _respond(None, scope=_scope())


def test_record_response_records_a_later_error_after_a_200():
    """Ollama's legacy fallback reuses one channel; the second fault counts."""
    backend = ma.admission_backend()
    scope = _scope()

    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope) as channel:
            channel.record_response(200)
            channel.record_response(503)

    asyncio.run(body())
    assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) > 0


def test_record_response_closes_the_circuit_only_once():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))

    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope) as channel:
            channel.record_response(200)
            assert backend.scope_cooldown_remaining(ma.DEFAULT_POOL, scope) == 0.0
            channel.record_response(200)

    asyncio.run(body())


def test_channel_exposes_its_scope_and_cleans_up_its_probe():
    backend = ma.admission_backend()
    scope = _scope()
    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope) as channel:
            assert channel.failure_scope == scope
        assert not backend.leases

    asyncio.run(body())


def test_channel_ignores_an_invalid_scope():
    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5,
                                    failure_scope="bogus") as channel:
            assert channel.failure_scope is None

    asyncio.run(body())


def test_channel_503_then_next_call_is_cooling():
    backend = ma.admission_backend()
    scope = _scope()
    _respond(503, scope=scope)

    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope):
            pytest.fail("a cooling scope must not send")

    with pytest.raises(AdmissionCooldownError):
        asyncio.run(body())


def test_cancellation_frees_the_slot_and_the_probe():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    cancelled = {"seen": False}

    async def body():
        nonlocal cancelled
        try:
            async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope):
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    loop = asyncio.new_event_loop()
    task = loop.create_task(body())
    loop.call_later(0.05, task.cancel)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    loop.close()

    assert cancelled["seen"] is True
    with backend._lock:
        assert not backend.leases, "no leaked slot or probe"
    # The circuit is still elapsed, so the next caller may probe.
    backend.release(_acquire(backend, scope=scope))


def test_a_busy_scope_reports_a_typed_cooldown_before_sending():
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 120)
    sent = {"count": 0}

    async def body():
        async with ma.async_channel(CHAT, hard_lifetime_seconds=5, failure_scope=scope) as channel:
            sent["count"] += 1

    with pytest.raises(AdmissionCooldownError):
        asyncio.run(body())
    assert sent["count"] == 0


def test_concurrent_callers_get_a_single_probe_across_threads():
    """Cross-process single-probe behaviour, exercised with real threads."""
    backend = ma.admission_backend()
    scope = _scope()
    backend.apply_scope_cooldown(ma.DEFAULT_POOL, scope, 60)
    _expire(backend, ma._scope_pool_id(scope))
    results, lock = [], threading.Lock()

    def attempt():
        try:
            lease = _acquire(backend, scope=scope, timeout=0.3)
        except AdmissionCooldownError:
            with lock:
                results.append("cooled")
        else:
            with lock:
                results.append("probed")
            time.sleep(0.05)
            backend.release(lease)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results.count("probed") == 1, results
    assert results.count("cooled") == 5, results
