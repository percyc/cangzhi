"""Request-local QA timings; no prompts, identities or provider payloads."""

import logging
from collections.abc import Awaitable
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from typing import TypeVar

logger = logging.getLogger(__name__)
_timings: ContextVar[dict[str, float] | None] = ContextVar("qa_timings", default=None)
T = TypeVar("T")


async def measure_qa(stage: str, operation: Awaitable[T]) -> T:
    started = perf_counter()
    try:
        return await operation
    finally:
        timings = _timings.get()
        if timings is not None:
            timings[stage] = timings.get(stage, 0) + round(
                (perf_counter() - started) * 1000, 2
            )


def qa_stage(stage: str):
    def decorate(fn):
        @wraps(fn)
        async def measured(*args, **kwargs):
            return await measure_qa(stage, fn(*args, **kwargs))

        return measured

    return decorate


def observe_qa(fn):
    @wraps(fn)
    async def measured(*args, **kwargs):
        timings: dict[str, float] = {}
        token = _timings.set(timings)
        status = "failed"
        started = perf_counter()
        try:
            result = await fn(*args, **kwargs)
            status = "completed"
            return result
        finally:
            _timings.reset(token)
            logger.info(
                "qa_timing status=%s total_ms=%.2f stages_ms=%s",
                status, (perf_counter() - started) * 1000, timings,
            )

    return measured
