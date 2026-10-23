"""Scoped admission regressions against an explicitly isolated PostgreSQL."""

import asyncio
import os
import uuid
from concurrent.futures import ProcessPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from apps.api.services import model_admission as ma
from apps.worker.services.monitoring import _model_pool_metrics


@pytest.fixture
def isolated_backend():
    url = os.getenv("CANGZHI_MODEL_ADMISSION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("explicit isolated database required")
    assert url.endswith("/cangzhi_test")
    engine = create_engine(url)
    backend = ma.PostgresAdmissionBackend(engine=engine)
    ma.configure_admission_backend(backend)
    pool = "q11-" + uuid.uuid4().hex
    scope = ma.failure_scope("chat", "https://synthetic.invalid/" + pool, "model", "test-only")
    yield backend, engine, pool, scope
    ma.reset_admission_backend()
    with engine.begin() as conn:
        for pid in (pool, "scope:" + scope):
            conn.execute(text("DELETE FROM model_admission_leases WHERE pool_id=:pid"), {"pid": pid})
            conn.execute(text("DELETE FROM model_admission_pools WHERE pool_id=:pid"), {"pid": pid})
    engine.dispose()


def expire_cooldown(engine, pid):
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_admission_pools SET cooldown_until=clock_timestamp()-interval '1 second' WHERE pool_id=:pid"), {"pid": pid})


def test_pg_scoped_failure_and_single_recovery_probe(isolated_backend):
    backend, engine, pool, scope = isolated_backend

    async def run():
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as channel:
            channel.record_response(503)
        with pytest.raises(ma.AdmissionCooldownError):
            async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
                pytest.fail("cooling model must not send")
        async with ma.async_channel(ma.EMBEDDING, pool_id=pool) as channel:
            channel.record_response(200)
        expire_cooldown(engine, "scope:" + scope)
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as probe:
            with pytest.raises(ma.AdmissionCooldownError):
                async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
                    pytest.fail("only one probe")
            probe.record_response(200)
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
            pass

    asyncio.run(run())


def test_pg_global_recovery_works_at_capacity_one(isolated_backend):
    backend, engine, pool, scope = isolated_backend
    backend.apply_cooldown(pool, 1)
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_admission_pools SET capacity=1, background_capacity=1 WHERE pool_id=:pid"), {"pid": pool})
    expire_cooldown(engine, pool)

    async def run():
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as probe:
            probe.record_response(200)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT cooldown_until FROM model_admission_pools WHERE pool_id=:pid"), {"pid": pool}) is None
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
            pass

    asyncio.run(run())


def test_pg_scope_refusal_does_not_strand_global_probe(isolated_backend):
    backend, engine, pool, scope = isolated_backend

    async def run():
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as request:
            request.record_response(503)
        backend.apply_cooldown(pool, 1)
        expire_cooldown(engine, pool)
        with pytest.raises(ma.AdmissionCooldownError):
            async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
                pytest.fail("scope remains cooling")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM model_admission_leases WHERE pool_id=:pid AND kind='probe'"), {"pid": pool}) == 0
        async with ma.async_channel(ma.EMBEDDING, pool_id=pool) as request:
            request.record_response(200)

    asyncio.run(run())


def test_pg_late_success_cannot_clear_newer_failure(isolated_backend):
    backend, engine, pool, scope = isolated_backend

    async def run():
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as old:
            async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as newer:
                newer.record_response(503)
            old.record_response(200)
        with pytest.raises(ma.AdmissionCooldownError):
            async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope):
                pytest.fail("late success cleared newer fault")

    asyncio.run(run())


def _claim_scoped_probe(url, pool, scope):
    backend = ma.PostgresAdmissionBackend(url)
    try:
        lease = backend.acquire(
            pool, ma.FOREGROUND, ma.CHAT, failure_scope=scope,
            lease_ttl_seconds=30, acquire_timeout_seconds=0.2,
        )
        return "ok", lease
    except ma.AdmissionCooldownError:
        return "cooldown", None


def test_pg_multiprocess_scope_recovery_has_one_probe(isolated_backend):
    backend, engine, pool, scope = isolated_backend

    async def fail():
        async with ma.async_channel(ma.CHAT, pool_id=pool, failure_scope=scope) as request:
            request.record_response(503)

    asyncio.run(fail())
    expire_cooldown(engine, "scope:" + scope)
    with ProcessPoolExecutor(max_workers=4) as workers:
        jobs = [workers.submit(_claim_scoped_probe, os.environ["CANGZHI_MODEL_ADMISSION_TEST_DATABASE_URL"], pool, scope) for _ in range(4)]
        outcomes = [job.result(timeout=15) for job in jobs]
    winners = [lease for status, lease in outcomes if status == "ok"]
    assert len(winners) == 1
    assert sum(status == "cooldown" for status, _ in outcomes) == 3
    assert backend.release(winners[0])


def test_pg_monitoring_shows_real_quota_not_virtual_circuits(isolated_backend):
    backend, engine, pool, scope = isolated_backend
    lease = backend.acquire(pool, ma.FOREGROUND, ma.CHAT, failure_scope=scope,
                            lease_ttl_seconds=30, acquire_timeout_seconds=0.2)
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_admission_pools SET background_capacity=capacity WHERE pool_id=:pid"), {"pid": pool})
    with Session(engine) as session:
        metrics = _model_pool_metrics(session)["model_pools"]
    assert all(not item["pool_id"].startswith("scope:") for item in metrics)
    item = next(item for item in metrics if item["pool_id"] == pool)
    assert item["background_capacity"] == item["capacity"] - 1
    assert item["in_use"] == 1
    assert backend.release(lease)


def test_pg_expired_probe_is_reaped_and_cannot_clear_successor(isolated_backend):
    backend, engine, pool, scope = isolated_backend
    backend.apply_scope_cooldown(pool, scope, 1)
    expire_cooldown(engine, "scope:" + scope)
    old = backend.acquire(pool, ma.FOREGROUND, ma.CHAT, failure_scope=scope,
                          lease_ttl_seconds=30, acquire_timeout_seconds=0.2)
    with engine.begin() as conn:
        for token in (old.owner_token, old.probes[0].token):
            conn.execute(text("UPDATE model_admission_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE owner_token=:token"), {"token": token})
    new = backend.acquire(pool, ma.FOREGROUND, ma.CHAT, failure_scope=scope,
                          lease_ttl_seconds=30, acquire_timeout_seconds=0.2)
    assert not backend.clear_probe_circuit(old.probes[0])
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM model_admission_leases WHERE pool_id=:pid AND kind='probe'"), {"pid": "scope:" + scope}) == 1
    assert backend.clear_probe_circuit(new.probes[0])
    assert backend.release(new)
