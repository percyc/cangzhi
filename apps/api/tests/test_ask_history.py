from __future__ import annotations

from apps.api.ai import AIProvider
from apps.api.api import v1 as v1_module
from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.main import app


class _ConfiguredProvider(AIProvider):
    name = "history-test"

    def is_configured(self) -> bool:
        return True

    def generate_understanding(self, **_kwargs):
        raise NotImplementedError

    def answer_question(self, **_kwargs):
        raise AssertionError("empty knowledge should not invoke synthesis")


def test_cloud_history_records_answer_without_enabling_memory(client, monkeypatch):
    test_client, _ = client
    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()
    setup = test_client.post(
        "/api/auth/setup",
        json={"username": "owner", "password": "a-strong-password"},
    )
    assert setup.status_code == 200

    created = test_client.post(
        "/api/ask/conversations",
        json={"title": "新问答"},
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    async def provider_from_db(_db):
        return _ConfiguredProvider()

    monkeypatch.setattr(v1_module, "build_provider_from_db", provider_from_db)
    answer = test_client.post(
        "/api/v1/knowledge/ask",
        json={
            "question": "没有资料时能回答吗？",
            "mode": "quick",
            "conversation_id": conversation_id,
            "context_label": "全部知识",
        },
    )
    assert answer.status_code == 200
    assert answer.json()["history"]["conversation_id"] == conversation_id

    listed = test_client.get("/api/ask/conversations")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["turn_count"] == 1
    assert listed.json()["items"][0]["title"] == "没有资料时能回答吗？"

    detail = test_client.get(f"/api/ask/conversations/{conversation_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["memory_enabled"] is False
    assert payload["turns"][0]["question"] == "没有资料时能回答吗？"
    assert payload["turns"][0]["context_label"] == "全部知识"
    assert payload["turns"][0]["response"]["insufficient_evidence"] is True

    renamed = test_client.patch(
        f"/api/ask/conversations/{conversation_id}",
        json={"title": "检索边界测试"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "检索边界测试"

    deleted = test_client.delete(f"/api/ask/conversations/{conversation_id}")
    assert deleted.status_code == 204
    missing = test_client.get(f"/api/ask/conversations/{conversation_id}")
    assert missing.status_code == 404
