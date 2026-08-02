"""Integration tests for the stable external knowledge API."""

import asyncio
import json

from apps.api.ai import AIProvider, AnswerResult
from apps.api.api import mcp as mcp_module
from apps.api.api import v1 as v1_module
from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import (
    Document,
    DocumentSourceType,
    DocumentVersion,
)


def _enable_real_login():
    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()


def _setup_owner(client):
    response = client.post(
        "/api/auth/setup",
        json={"username": "owner", "password": "a-strong-password"},
    )
    assert response.status_code == 200


def test_access_token_lifecycle_and_scope_enforcement(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)

    created = test_client.post(
        "/api/access-tokens",
        json={
            "name": "Hermes",
            "scopes": ["knowledge:read", "knowledge:search"],
        },
    )
    assert created.status_code == 201
    token = created.json()["token"]
    item = created.json()["item"]
    assert token.startswith("cz_pat_")
    assert token not in str(item)
    integration = created.json()["integration"]
    assert integration["api_path"] == "/api/v1"
    assert integration["mcp_path"] == "/api/mcp"
    assert integration["authorization_header"] == f"Bearer {token}"

    listed = test_client.get("/api/access-tokens")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["token_prefix"] == token[:15]
    assert token not in listed.text

    test_client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    capabilities = test_client.get("/api/v1/capabilities", headers=headers)
    assert capabilities.status_code == 200
    assert capabilities.json()["api_version"] == "v1"
    assert capabilities.json()["features"]["facets"] is True

    facets = test_client.get("/api/v1/knowledge/facets", headers=headers)
    assert facets.status_code == 200
    assert set(facets.json()) == {
        "categories",
        "tags",
        "source_types",
        "connectors",
    }

    search = test_client.post(
        "/api/v1/knowledge/search",
        headers=headers,
        json={"query": "不存在的资料"},
    )
    assert search.status_code == 200
    assert search.json()["scope"]["slug"] == "all"
    assert search.json()["hits"] == []

    initialized = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "cangzhi"
    tools = test_client.post(
        "/api/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert {item["name"] for item in tools.json()["result"]["tools"]} == {
        "knowledge_list_scopes",
        "knowledge_list_facets",
        "knowledge_search",
        "knowledge_ask",
        "knowledge_get_document",
        "knowledge_get_chunk",
        "knowledge_list_datasets",
        "knowledge_get_dataset_schema",
        "knowledge_preview_dataset_rows",
        "knowledge_query_dataset",
        "knowledge_get_evidence_by_chunk",
        "knowledge_get_evidence_by_dataset",
        "knowledge_preview_evidence_rows",
    }
    query_tool = next(
        item
        for item in tools.json()["result"]["tools"]
        if item["name"] == "knowledge_query_dataset"
    )
    filter_schema = query_tool["inputSchema"]["$defs"]["DatasetFilterInput"]
    assert set(filter_schema["properties"]) == {"column", "operator", "value"}
    assert filter_schema["properties"]["operator"]["enum"] == [
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "starts_with",
        "ends_with",
        "direct_child_of",
        "in",
    ]
    ask_tool = next(
        item
        for item in tools.json()["result"]["tools"]
        if item["name"] == "knowledge_ask"
    )
    assert "mode" not in ask_tool["inputSchema"]["properties"]
    assert ask_tool["inputSchema"]["additionalProperties"] is False
    mcp_search = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "knowledge_search",
                "arguments": {"query": "不存在的资料", "scope_slug": "all"},
            },
        },
    )
    tool_result = mcp_search.json()["result"]
    assert tool_result["isError"] is False
    assert tool_result["structuredContent"]["hits"] == []
    mcp_ask = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "knowledge_ask",
                "arguments": {"question": "这是什么？", "scope_slug": "all"},
            },
        },
    ).json()["result"]
    assert mcp_ask["isError"] is True
    assert mcp_ask["structuredContent"]["error"]["code"] == "insufficient_scope"
    mcp_facets = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "knowledge_list_facets",
                "arguments": {},
            },
        },
    ).json()["result"]
    assert mcp_facets["isError"] is False
    assert set(mcp_facets["structuredContent"]) == {
        "categories",
        "tags",
        "source_types",
        "connectors",
    }

    ask = test_client.post(
        "/api/v1/knowledge/ask",
        headers=headers,
        json={"question": "这是什么？"},
    )
    assert ask.status_code == 403
    assert ask.json()["detail"]["code"] == "insufficient_scope"

    # Revocation remains cookie-only and immediately invalidates external use.
    test_client.cookies.clear()
    login = test_client.post(
        "/api/auth/login",
        json={"username": "owner", "password": "a-strong-password"},
    )
    assert login.status_code == 200
    active_delete = test_client.delete(f"/api/access-tokens/{item['id']}")
    assert active_delete.status_code == 409
    assert active_delete.json()["detail"]["code"] == "token_must_be_revoked"
    revoked = test_client.post(f"/api/access-tokens/{item['id']}/revoke")
    assert revoked.status_code == 200
    deleted = test_client.delete(f"/api/access-tokens/{item['id']}")
    assert deleted.status_code == 204
    assert test_client.get("/api/access-tokens").json()["items"] == []
    test_client.cookies.clear()
    assert test_client.get("/api/v1/capabilities", headers=headers).status_code == 401


def test_document_and_chunk_read_only_expose_current_active_knowledge(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            document = Document(
                title="中枢设计",
                source_type=DocumentSourceType.note,
                is_deleted=False,
            )
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="a" * 64,
                raw_content="藏知是个人知识中枢。",
                structured_content={"document_type": "note", "blocks": []},
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            chunk = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                external_id="chunk-1",
                role="child",
                chunk_type="paragraph",
                order_index=0,
                content="藏知是个人知识中枢。",
                search_text="中枢设计 藏知是个人知识中枢。",
                content_hash="b" * 64,
                heading_path=[],
                char_count=11,
                token_estimate=6,
                is_current=True,
            )
            db.add(chunk)
            await db.commit()
            return document.id, chunk.id
        raise AssertionError("database override missing")

    document_id, chunk_id = asyncio.run(seed())
    document = test_client.get(f"/api/v1/knowledge/documents/{document_id}")
    assert document.status_code == 200
    assert document.json()["content"] == "藏知是个人知识中枢。"
    assert document.json()["version"]["content_hash"] == "a" * 64

    chunk = test_client.get(f"/api/v1/knowledge/chunks/{chunk_id}")
    assert chunk.status_code == 200
    assert chunk.json()["content_hash"] == "b" * 64

    mcp_document = test_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "knowledge_get_document",
                "arguments": {
                    "document_id": document_id,
                    "offset": 0,
                    "max_chars": 5,
                },
            },
        },
    ).json()["result"]["structuredContent"]
    assert mcp_document["content"] == "藏知是个人"
    assert mcp_document["content_window"] == {
        "offset": 0,
        "returned_chars": 5,
        "total_chars": 10,
        "truncated": True,
        "next_offset": 5,
        "max_chars": 5,
    }
    assert "structured_content" not in mcp_document
    assert mcp_document["structure"]["document_type"] == "note"


def test_mcp_knowledge_ask_uses_shared_cited_qa(client, monkeypatch):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)
    created = test_client.post(
        "/api/access-tokens",
        json={
            "name": "MCP Ask",
            "scopes": ["knowledge:read", "knowledge:search", "knowledge:ask"],
        },
    )
    token = created.json()["token"]

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            document = Document(
                title="中枢设计",
                source_type=DocumentSourceType.note,
                is_deleted=False,
            )
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="mcp-ask-version",
                raw_content="藏知是个人知识中枢。",
                structured_content={"document_type": "note", "blocks": []},
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            db.add(
                DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    external_id="mcp-ask-chunk",
                    role="child",
                    chunk_type="paragraph",
                    order_index=0,
                    content="藏知是个人知识中枢。",
                    search_text="中枢设计 藏知是个人知识中枢。",
                    content_hash="mcp-ask-chunk-hash",
                    heading_path=[],
                    char_count=11,
                    token_estimate=6,
                    is_current=True,
                )
            )
            await db.commit()
            return

    asyncio.run(seed())

    class Provider(AIProvider):
        name = "stub"

        def is_configured(self):
            return True

        def generate_understanding(self, **_):
            raise NotImplementedError

        def answer_question(self, *, question, evidence):
            return AnswerResult(
                answer="藏知是个人知识中枢。",
                citation_ids=[int(evidence[0]["id"])],
                insufficient_evidence=False,
            )

    async def provider_from_db(_db):
        return Provider()

    monkeypatch.setattr(mcp_module, "build_provider_from_db", provider_from_db)
    test_client.cookies.clear()
    response = test_client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "knowledge_ask",
                "arguments": {"question": "藏知是什么？"},
            },
        },
    )

    tool_result = response.json()["result"]
    assert tool_result["isError"] is False
    assert tool_result["structuredContent"]["answer"] == "藏知是个人知识中枢。"
    assert tool_result["structuredContent"]["citations"][0]["title"] == "中枢设计"

    nested_deep = test_client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "knowledge_ask",
                "arguments": {
                    "question": "藏知是什么？",
                    "mode": "deep",
                },
            },
        },
    ).json()["result"]
    assert nested_deep["isError"] is True
    assert nested_deep["structuredContent"]["error"]["code"] == "invalid_arguments"


def test_deep_ask_stream_emits_live_tool_progress_and_result(client, monkeypatch):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            document = Document(
                title="流式问答资料", source_type=DocumentSourceType.note
            )
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="streaming-answer-version",
                raw_content="流式问答会实时展示工具进度。",
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            db.add(
                DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    external_id="streaming-answer-chunk",
                    role="child",
                    chunk_type="paragraph",
                    order_index=0,
                    content="流式问答会实时展示工具进度。",
                    search_text="流式问答 实时展示 工具进度",
                    content_hash="streaming-answer-chunk-hash",
                    heading_path=[],
                    char_count=15,
                    token_estimate=8,
                    is_current=True,
                )
            )
            await db.commit()
            return

    asyncio.run(seed())

    class Provider(AIProvider):
        name = "stub"

        def __init__(self):
            self.decisions = [
                {"action": "search", "query": "流式问答 工具进度"},
                {"action": "finish", "summary": "证据充分"},
            ]

        def is_configured(self):
            return True

        def generate_understanding(self, **_):
            raise NotImplementedError

        def generate_json(self, **_):
            return self.decisions.pop(0)

        def answer_question(self, *, question, evidence):
            return AnswerResult(
                answer="流式问答会实时展示工具进度。",
                citation_ids=[int(evidence[0]["id"])],
                insufficient_evidence=False,
            )

    async def provider_from_db(_db):
        return Provider()

    monkeypatch.setattr(v1_module, "build_provider_from_db", provider_from_db)
    with test_client.stream(
        "POST",
        "/api/v1/knowledge/ask/stream",
        json={"question": "流式问答如何展示进度？", "mode": "deep"},
    ) as response:
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert events[0]["phase"] == "starting"
    assert events[0]["max_tool_calls"] == 12
    tool_event = next(item for item in events if item.get("phase") == "tool")
    assert tool_event["step"]["tool"] == "knowledge_search"
    assert tool_event["tool_calls"] == 1
    assert events[-1]["type"] == "result"
    assert events[-1]["data"]["answer"] == "流式问答会实时展示工具进度。"


def test_missing_saved_scope_never_broadens_to_all(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)
    response = test_client.post(
        "/api/v1/knowledge/search",
        json={"query": "任意", "scope_slug": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "scope_not_found"
