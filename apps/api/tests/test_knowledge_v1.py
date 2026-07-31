"""Integration tests for the stable external knowledge API."""

import asyncio

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
    assert {
        item["name"] for item in tools.json()["result"]["tools"]
    } == {
        "knowledge_list_scopes",
        "knowledge_search",
        "knowledge_get_document",
        "knowledge_get_chunk",
    }
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
    assert (
        test_client.get("/api/v1/capabilities", headers=headers).status_code
        == 401
    )


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
