"""Independent durable scan loop; slow document parsing cannot starve scans."""
import asyncio
import threading

import httpx
import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.services.webdav_scan_jobs import process_scan_batch

logger = structlog.get_logger()


async def scan_loop(database_url, stop):
    url = make_url(database_url)
    url = url.set(drivername="sqlite+aiosqlite" if url.get_backend_name() == "sqlite" else "postgresql+asyncpg")
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                    limits=httpx.Limits(max_connections=3, max_keepalive_connections=3)) as client:
            while not stop.is_set():
                try:
                    worked = await process_scan_batch(sessions, client=client)
                except Exception as exc:
                    logger.warning("webdav_scan_batch_failed", error_type=type(exc).__name__)
                    worked = False
                # Rate-limit batch turnover, also yield between sources.
                for _ in range(1 if worked else 10):
                    if stop.is_set():
                        break
                    await asyncio.sleep(0.2)
    finally:
        await engine.dispose()


def start_scanner(database_url):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: asyncio.run(scan_loop(database_url, stop)),
                              name="webdav-scanner", daemon=True)
    thread.start()
    return stop, thread
