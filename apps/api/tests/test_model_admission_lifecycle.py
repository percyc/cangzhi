"""Independent transport/cleanup regressions; only local fake HTTP and test DB."""
import asyncio
import threading
import time
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest

from apps.api.services import model_admission as ma


@pytest.mark.parametrize("module", ["apps.api.ai.provider", "apps.worker.main",
    "apps.worker.services.task_execution", "apps.api.services.model_admission"])
def test_cold_import(module):
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True, timeout=20)


@pytest.fixture(autouse=True)
def isolated_backend():
    backend = ma.InMemoryAdmissionBackend()
    ma.configure_admission_backend(backend)
    yield backend
    ma.reset_admission_backend()


def test_separate_pools_and_owner_guard(isolated_backend):
    def acquire(pool):
        return isolated_backend.acquire(pool, ma.FOREGROUND, ma.CHAT,
            lease_ttl_seconds=5, acquire_timeout_seconds=.1)
    first, second = acquire("a"), acquire("a")
    other = acquire("b")
    assert not isolated_backend.release(replace(first, pool_id="b"))
    for holder in (first, second, other):
        assert isolated_backend.release(holder)


def test_waiter_does_not_lock_out_releaser(isolated_backend):
    def acquire():
        return isolated_backend.acquire("a", ma.BACKGROUND, ma.CHAT,
            lease_ttl_seconds=5, acquire_timeout_seconds=.8)
    held = acquire()
    result = []
    thread = threading.Thread(target=lambda: result.append(acquire()))
    thread.start()
    time.sleep(.05)
    isolated_backend.release(held)
    thread.join(1)
    assert len(result) == 1
    isolated_backend.release(result[0])


def test_cancel_during_acquire_cleans_before_loop_closes(monkeypatch, isolated_backend):
    original = isolated_backend.acquire
    entered = threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        time.sleep(.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(isolated_backend, "acquire", slow)
    async def scenario():
        async def work():
            async with ma.async_channel(ma.CHAT):
                pytest.fail("cancelled acquire must never send")
        task = asyncio.create_task(work())
        while not entered.is_set():
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()  # cleanup itself must survive another cancellation
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert not isolated_backend.leases


def test_release_failure_preserves_success(monkeypatch, isolated_backend):
    def unavailable(_):
        raise RuntimeError("synthetic coordinator outage")
    monkeypatch.setattr(isolated_backend, "release", unavailable)
    async def scenario():
        async with ma.async_channel(ma.CHAT) as channel:
            return await channel.run(asyncio.sleep(0, result="answer"))
    assert asyncio.run(scenario()) == "answer"
    assert len(isolated_backend.leases) == 1  # conservative until finite expiry


def test_absolute_deadline_does_not_refund_prior_request(isolated_backend):
    async def scenario():
        async with ma.async_channel(ma.CHAT, hard_lifetime_seconds=1) as channel:
            assert await channel.run(asyncio.sleep(0, result="sent")) == "sent"
            channel._deadline = time.monotonic() - 1
            with pytest.raises(TimeoutError):
                await channel.run(asyncio.sleep(0))
    asyncio.run(scenario())
    assert not isolated_backend.leases


@pytest.mark.parametrize("cancel", [False, True])
def test_real_http_slow_body_closed_before_reuse(cancel, isolated_backend):
    """Read timeout is refreshed by bytes, but the absolute deadline still wins."""
    async def scenario():
        connected, disconnected = asyncio.Event(), asyncio.Event()
        handlers = set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                await writer.drain()
                connected.set()
                while True:
                    writer.write(b"x")
                    await writer.drain()
                    try:
                        eof = await asyncio.wait_for(reader.read(1), timeout=.05)
                    except asyncio.TimeoutError:
                        continue
                    if not eof:
                        break
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                disconnected.set()
                handlers.discard(task)
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async def request():
            async with ma.async_channel(ma.CHAT, hard_lifetime_seconds=1) as channel:
                async def transport():
                    async with httpx.AsyncClient(timeout=.3, trust_env=False) as client:
                        return await client.get(f"http://127.0.0.1:{port}/")
                return await channel.run(transport())
        try:
            started = time.monotonic()
            task = asyncio.create_task(request())
            await asyncio.wait_for(connected.wait(), 2)
            assert len(isolated_backend.leases) == 1
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            await asyncio.wait_for(disconnected.wait(), 1)
            assert not isolated_backend.leases
            assert time.monotonic() - started < 2.5
            async with ma.async_channel(ma.CHAT):
                assert len(isolated_backend.leases) == 1
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
    asyncio.run(scenario())
