"""Real transport wiring, using synthetic HTTP responses and shared admission."""

import asyncio
import json

import httpx
import pytest

from apps.api.ai.provider import _post_chat_async
from apps.api.services import model_admission as ma
from apps.api.services.hybrid_retrieval import _request_query_embedding


@pytest.fixture(autouse=True)
def admission_backend():
    backend = ma.InMemoryAdmissionBackend()
    ma.configure_admission_backend(backend)
    ma.set_process_role(ma.FOREGROUND)
    yield backend
    ma.set_process_role(ma.FOREGROUND)
    ma.reset_admission_backend()


@pytest.mark.parametrize("status", [401, 403, 502, 504])
def test_background_model_failure_leaves_other_chat_and_query_embedding_available(monkeypatch, status):
    sent = []

    def respond(request):
        model = json.loads(request.content)["model"]
        sent.append(model)
        if model == "broken":
            return httpx.Response(status, json={"error": "synthetic"})
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond)))

    async def run():
        ma.set_process_role(ma.BACKGROUND)
        response = await _post_chat_async(
            "https://synthetic.invalid/chat/completions", {"model": "broken"}, {},
            timeout=5, trust_env=False,
        )
        assert response.status_code == status
        ma.set_process_role(ma.FOREGROUND)
        response = await _post_chat_async(
            "https://synthetic.invalid/chat/completions", {"model": "healthy"}, {},
            timeout=5, trust_env=False,
        )
        assert response.status_code == 200
        vector = await _request_query_embedding(
            provider="openai", base_url="https://synthetic.invalid", model="embedding",
            api_key="", timeout_seconds=5, text="synthetic question",
        )
        assert vector == [0.1, 0.2]
        with pytest.raises(ma.AdmissionCooldownError):
            await _post_chat_async(
                "https://synthetic.invalid/chat/completions", {"model": "broken"}, {},
                timeout=5, trust_env=False,
            )

    asyncio.run(run())
    assert sent == ["broken", "healthy", "embedding"]


def test_real_pool_rate_limit_is_not_bypassed_by_foreground_model_switch(monkeypatch):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond)))

    async def run():
        ma.set_process_role(ma.BACKGROUND)
        await _post_chat_async("https://synthetic.invalid/chat", {"model": "a"}, {}, timeout=5, trust_env=False)
        ma.set_process_role(ma.FOREGROUND)
        with pytest.raises(ma.AdmissionCooldownError):
            await _post_chat_async("https://synthetic.invalid/chat", {"model": "b"}, {}, timeout=5, trust_env=False)

    asyncio.run(run())
    assert len(sent) == 1
