import asyncio
import logging

import pytest

from apps.api.services.qa_timing import measure_qa, observe_qa, _timings


def test_timings_isolate_concurrent_requests_and_never_log_inputs(caplog):
    @observe_qa
    async def ask(stage, secret):
        await measure_qa(stage, asyncio.sleep(0))
        return secret

    async def run():
        assert await asyncio.gather(ask("retrieval", "private-question"),
                                    ask("answer_generation", "private-key")) == [
                                        "private-question", "private-key"]
        assert _timings.get() is None

    with caplog.at_level(logging.INFO, logger="apps.api.services.qa_timing"):
        asyncio.run(run())
    assert len(caplog.records) == 2
    assert "private" not in caplog.text
    assert all("completed" in record.message for record in caplog.records)
    assert all(not ("retrieval" in record.message and "answer_generation" in record.message)
               for record in caplog.records)


@pytest.mark.parametrize("error", [ValueError, asyncio.CancelledError])
def test_timings_record_failure_and_reset_context(caplog, error):
    @observe_qa
    async def ask():
        async def fail():
            raise error("sensitive upstream response")
        await measure_qa("answer_generation", fail())

    async def run():
        with pytest.raises(error):
            await ask()
        assert _timings.get() is None

    with caplog.at_level(logging.INFO, logger="apps.api.services.qa_timing"):
        asyncio.run(run())
    assert "failed" in caplog.text
    assert "answer_generation" in caplog.text
    assert "sensitive" not in caplog.text
