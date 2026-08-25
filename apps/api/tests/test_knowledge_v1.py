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
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.models.documents import (
    Document,
    DocumentSourceType,
    DocumentVersion,
)
from apps.api.models.exploration_grants import ExplorationGrant


def test_mcp_search_accepts_dify_serialized_document_selection():
    args = mcp_module.SearchArguments.model_validate(
        {
            "query": "搜索",
            "document_selection": (
                '{"scope_keys": ["kb-oai-2089967679615209472"]}\n'
            ),
        }
    )

    assert args.document_selection is not None
    assert args.document_selection.scope_keys == ["kb-oai-2089967679615209472"]


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
        "knowledge_list_documents",
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
    search_tool = next(
        item
        for item in tools.json()["result"]["tools"]
        if item["name"] == "knowledge_search"
    )
    search_properties = search_tool["inputSchema"]["properties"]
    assert "document_selection" in search_properties
    assert "access_key" not in search_properties
    assert "document_ids" not in search_properties
    selection_schema = search_tool["inputSchema"]["$defs"][
        "DocumentSelectionArguments"
    ]
    assert selection_schema["additionalProperties"] is False
    assert set(selection_schema["properties"]) == {"scope_keys", "document_ids"}
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
    dify_mcp_search = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {
                "name": "knowledge_search",
                "arguments": {
                    "query": "不存在的资料",
                    "scope_slug": "all",
                    "document_selection": (
                        '{"scope_keys": ["kb-oai-2089967679615209472"]}\n'
                    ),
                },
            },
        },
    ).json()["result"]
    assert dify_mcp_search["isError"] is False
    assert dify_mcp_search["structuredContent"]["hits"] == []
    legacy_selection = test_client.post(
        "/api/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "knowledge_search",
                "arguments": {
                    "query": "不存在的资料",
                    "access_key": "legacy",
                },
            },
        },
    ).json()["result"]
    assert legacy_selection["isError"] is True
    assert legacy_selection["structuredContent"]["error"]["code"] == (
        "invalid_arguments"
    )
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


def test_openapi_exposes_bearer_authorization_for_external_api_debugging(client):
    test_client, _ = client
    schema = test_client.get("/openapi.json").json()
    bearer = schema["components"]["securitySchemes"]["CangzhiBearer"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert schema["paths"]["/api/v1/exploration-grants"]["post"]["security"] == [
        {"CangzhiBearer": []}
    ]
    assert schema["paths"]["/api/mcp"]["post"]["security"] == [
        {"CangzhiBearer": []}
    ]


def test_bound_token_uploads_and_manages_scope_keys(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)
    current = test_client.get("/api/workspaces/current").json()
    created = test_client.post(
        "/api/access-tokens",
        json={
            "name": "External uploader",
            "scopes": ["knowledge:read", "knowledge:search", "documents:write"],
            "workspace_id": current["id"],
        },
    )
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["item"]["workspace_id"] == current["id"]
    test_client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    uploaded = test_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("guide.txt", b"scope key integration", "text/plain")},
        data={"external_id": "ext-guide", "scope_keys": '["id-a", "id-b"]'},
    )
    assert uploaded.status_code == 202
    body = uploaded.json()
    assert body["status"] == "queued"
    assert body["scope_keys"] == ["id-a", "id-b"]
    document_id = body["document_id"]
    assert body["status_url"] == f"/api/v1/documents/{document_id}/processing-status"
    processing = test_client.get(body["status_url"], headers=headers)
    assert processing.status_code == 200
    assert processing.json()["document_id"] == document_id
    assert processing.json()["overall_status"] == "processing"
    replaced = test_client.put(
        f"/api/v1/documents/{document_id}/scope-keys",
        headers=headers,
        json={"scope_keys": ["id-c"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["scope_keys"] == ["id-c"]
    unchanged = test_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("guide.txt", b"scope key integration", "text/plain")},
        data={"external_id": "ext-guide"},
    )
    assert unchanged.status_code == 202
    assert unchanged.json()["status"] == "unchanged"
    assert unchanged.json()["document_id"] == document_id
    assert unchanged.json()["scope_keys"] == ["id-c"]
    selected = test_client.post(
        "/api/v1/knowledge/search",
        headers=headers,
        json={
            "query": "integration",
            "document_selection": {
                "scope_keys": ["id-c"],
                "document_ids": [],
            },
        },
    )
    assert selected.status_code == 200
    empty_selection = test_client.post(
        "/api/v1/knowledge/search",
        headers=headers,
        json={"query": "integration", "document_selection": {}},
    )
    assert empty_selection.status_code == 400
    invalid_batch = test_client.put(
        "/api/v1/document-scope-keys/batch",
        headers=headers,
        json={"items": [{"document_id": document_id, "scope_keys": [""]}]},
    )
    assert invalid_batch.status_code == 400


def test_bound_token_cannot_switch_workspace(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)
    current = test_client.get("/api/workspaces/current").json()
    assert test_client.post(
        "/api/workspaces", json={"slug": "other", "name": "其他空间"}
    ).status_code == 201
    created = test_client.post(
        "/api/access-tokens",
        json={"name": "Bound", "scopes": ["knowledge:read"], "workspace_id": current["id"]},
    )
    token = created.json()["token"]
    test_client.cookies.clear()
    response = test_client.get(
        "/api/v1/capabilities",
        headers={"Authorization": f"Bearer {token}", "X-Cangzhi-Workspace": "other"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "workspace_token_mismatch"


def test_exploration_grant_supports_rest_and_mcp_without_escaping_boundary(client):
    test_client, _ = client
    _enable_real_login()
    _setup_owner(test_client)
    workspace = test_client.get("/api/workspaces/current").json()
    created = test_client.post(
        "/api/access-tokens",
        json={
            "name": "Third-party backend",
            "scopes": ["knowledge:read", "knowledge:search", "knowledge:ask"],
            "workspace_id": workspace["id"],
        },
    )
    pat = created.json()["token"]

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            documents = []
            chunks = []
            for index, title in enumerate(("允许文档", "边界外文档"), start=1):
                document = Document(
                    title=title,
                    source_type=DocumentSourceType.note,
                    is_deleted=False,
                )
                db.add(document)
                await db.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    content_hash=str(index) * 64,
                    raw_content=f"共同检索词 {title}",
                    processing_status="ready",
                )
                db.add(version)
                await db.flush()
                document.current_version_id = version.id
                chunk = DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    external_id=f"grant-chunk-{index}",
                    role="child",
                    chunk_type="paragraph",
                    order_index=0,
                    content=f"共同检索词 {title}",
                    search_text=f"共同检索词 {title}",
                    content_hash=f"{index + 2}" * 64,
                    heading_path=[],
                    char_count=10,
                    token_estimate=5,
                    is_current=True,
                )
                db.add(chunk)
                await db.flush()
                documents.append(document.id)
                chunks.append(chunk.id)
            db.add(
                DocumentScopeKey(
                    workspace_id=workspace["id"],
                    document_id=documents[0],
                    scope_key="external-user-a",
                )
            )
            await db.commit()
            return documents, chunks
        raise AssertionError("database override missing")

    document_ids, chunk_ids = asyncio.run(seed())
    test_client.cookies.clear()
    pat_headers = {"Authorization": f"Bearer {pat}"}
    grant_response = test_client.post(
        "/api/v1/exploration-grants",
        headers=pat_headers,
        json={
            "document_selection": {"scope_keys": ["external-user-a"]},
            "ttl_seconds": 600,
        },
    )
    assert grant_response.status_code == 201
    grant_token = grant_response.json()["token"]
    grant_item = grant_response.json()["grant"]
    assert grant_token.startswith("cz_eg_")
    assert grant_token not in str(grant_item)
    assert grant_item["scope_key_count"] == 1
    assert "scope_keys" not in grant_item

    async def assert_stored_as_hash():
        async for db in app.dependency_overrides[get_db]():
            stored = await db.get(ExplorationGrant, grant_item["id"])
            assert stored is not None
            assert stored.token_hash != grant_token
            assert grant_token.startswith(stored.token_prefix)
            return
        raise AssertionError("database override missing")

    asyncio.run(assert_stored_as_hash())
    grant_headers = {"Authorization": f"Bearer {grant_token}"}
    capabilities = test_client.get("/api/v1/capabilities", headers=grant_headers)
    assert capabilities.status_code == 200
    assert capabilities.json()["features"]["rest_exploration_grants"] is True

    rest_allowed = test_client.get(
        f"/api/v1/knowledge/documents/{document_ids[0]}", headers=grant_headers
    )
    assert rest_allowed.status_code == 200
    grant_catalog = test_client.get(
        "/api/v1/knowledge/documents", headers=grant_headers
    )
    assert grant_catalog.status_code == 200
    assert grant_catalog.json()["total"] == 1
    assert [item["id"] for item in grant_catalog.json()["items"]] == [
        document_ids[0]
    ]
    assert "scope_keys" not in grant_catalog.json()["items"][0]
    assert "content" not in grant_catalog.json()["items"][0]
    escaped_catalog = test_client.get(
        "/api/v1/knowledge/documents",
        headers=grant_headers,
        params={"document_ids": document_ids[1]},
    )
    assert escaped_catalog.status_code == 200
    assert escaped_catalog.json()["total"] == 0
    pat_catalog = test_client.get(
        "/api/v1/knowledge/documents",
        headers=pat_headers,
        params={"scope_keys": "external-user-a"},
    )
    assert pat_catalog.status_code == 200
    assert pat_catalog.json()["total"] == 1
    assert pat_catalog.json()["items"][0]["id"] == document_ids[0]
    rest_escaped = test_client.get(
        f"/api/v1/knowledge/documents/{document_ids[1]}", headers=grant_headers
    )
    assert rest_escaped.status_code == 404
    rest_search = test_client.post(
        "/api/v1/knowledge/search",
        headers=grant_headers,
        json={"query": "共同检索词"},
    )
    assert rest_search.status_code == 200
    assert {hit["document_id"] for hit in rest_search.json()["hits"]} <= {
        document_ids[0]
    }
    rest_narrowed = test_client.post(
        "/api/v1/knowledge/search",
        headers=grant_headers,
        json={
            "query": "共同检索词",
            "document_selection": {"document_ids": [document_ids[1]]},
        },
    )
    assert rest_narrowed.status_code == 200
    assert rest_narrowed.json()["hits"] == []
    history_denied = test_client.post(
        "/api/v1/knowledge/ask",
        headers=grant_headers,
        json={"question": "共同检索词", "conversation_id": 1},
    )
    assert history_denied.status_code == 403
    assert history_denied.json()["detail"]["code"] == (
        "conversation_history_not_allowed"
    )
    write_denied = test_client.put(
        f"/api/v1/documents/{document_ids[0]}/scope-keys",
        headers=grant_headers,
        json={"scope_keys": ["forbidden"]},
    )
    assert write_denied.status_code == 403
    assert write_denied.json()["detail"]["code"] == "credential_not_allowed"
    mint_denied = test_client.post(
        "/api/v1/exploration-grants",
        headers=grant_headers,
        json={"document_selection": {"document_ids": [document_ids[0]]}},
    )
    assert mint_denied.status_code == 403
    assert mint_denied.json()["detail"]["code"] == "pat_required"

    def call_tool(name, arguments, headers):
        return test_client.post(
            "/api/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ).json()["result"]

    allowed = call_tool(
        "knowledge_get_document", {"document_id": document_ids[0]}, grant_headers
    )
    assert allowed["isError"] is False
    listed = call_tool("knowledge_list_documents", {}, grant_headers)
    assert listed["isError"] is False
    assert listed["structuredContent"]["total"] == 1
    assert [item["id"] for item in listed["structuredContent"]["items"]] == [
        document_ids[0]
    ]
    narrowed_list = call_tool(
        "knowledge_list_documents",
        {"document_selection": {"document_ids": [document_ids[1]]}},
        grant_headers,
    )
    assert narrowed_list["structuredContent"]["total"] == 0
    escaped_document = call_tool(
        "knowledge_get_document", {"document_id": document_ids[1]}, grant_headers
    )
    assert escaped_document["isError"] is True
    assert escaped_document["structuredContent"]["error"]["code"] == "document_not_found"
    escaped_chunk = call_tool(
        "knowledge_get_chunk", {"chunk_id": chunk_ids[1]}, grant_headers
    )
    assert escaped_chunk["isError"] is True
    # A per-call selector can only narrow the immutable grant boundary.
    narrowed = call_tool(
        "knowledge_search",
        {
            "query": "共同检索词",
            "document_selection": {"document_ids": [document_ids[1]]},
        },
        grant_headers,
    )
    assert narrowed["structuredContent"]["hits"] == []
    # The original PAT keeps full-workspace MCP behavior.
    pat_read = call_tool(
        "knowledge_get_document", {"document_id": document_ids[1]}, pat_headers
    )
    assert pat_read["isError"] is False

    revoked = test_client.post(
        f"/api/v1/exploration-grants/{grant_item['id']}/revoke",
        headers=pat_headers,
    )
    assert revoked.status_code == 200
    assert (
        test_client.post(
            "/api/mcp",
            headers=grant_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ).status_code
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


def _setup_mcp_ask_env(client, monkeypatch):
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
    return test_client, token


def _mcp_ask_body(question="藏知是什么？", progress_token=None, extra=None):
    params = {"name": "knowledge_ask", "arguments": {"question": question}}
    if progress_token is not None:
        params["_meta"] = {"progressToken": progress_token}
    if extra:
        params["arguments"].update(extra)
    return {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": params}


def _parse_sse(body):
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


def test_mcp_knowledge_ask_uses_shared_cited_qa(client, monkeypatch):
    test_client, token = _setup_mcp_ask_env(client, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}

    response = test_client.post("/api/mcp", headers=headers, json=_mcp_ask_body())
    tool_result = response.json()["result"]
    assert tool_result["isError"] is False
    assert tool_result["structuredContent"]["answer"] == "藏知是个人知识中枢。"
    assert tool_result["structuredContent"]["citations"][0]["title"] == "中枢设计"

    nested_deep = test_client.post(
        "/api/mcp",
        headers=headers,
        json=_mcp_ask_body(extra={"mode": "deep"}),
    ).json()["result"]
    assert nested_deep["isError"] is True
    assert nested_deep["structuredContent"]["error"]["code"] == "invalid_arguments"


def test_mcp_knowledge_ask_sse_streams_progress_then_result(client, monkeypatch):
    test_client, token = _setup_mcp_ask_env(client, monkeypatch)
    with test_client.stream(
        "POST",
        "/api/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json=_mcp_ask_body(progress_token="pt-42"),
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        response.read()
        events = _parse_sse(response.text)

    progress = [e for e in events if e.get("method") == "notifications/progress"]
    assert len(progress) == 5
    for event in progress:
        assert event["jsonrpc"] == "2.0"
        assert event["params"]["progressToken"] == "pt-42"
        assert event["params"]["total"] == 100
        assert isinstance(event["params"]["message"], str) and event["params"]["message"]
    values = [event["params"]["progress"] for event in progress]
    assert values == [0, 40, 70, 90, 100]
    assert events[-1]["id"] == 8
    assert events[-1]["result"]["isError"] is False
    assert events[-1]["result"]["structuredContent"]["answer"] == "藏知是个人知识中枢。"


def test_mcp_knowledge_ask_sse_token_and_legacy_compatibility(client, monkeypatch):
    test_client, token = _setup_mcp_ask_env(client, monkeypatch)
    with test_client.stream(
        "POST",
        "/api/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "APPLICATION/JSON, TEXT/EVENT-STREAM",
        },
        json=_mcp_ask_body(progress_token=7),
    ) as response:
        response.read()
        events = _parse_sse(response.text)
    progress = [e for e in events if e.get("method") == "notifications/progress"]
    assert progress
    assert all(e["params"]["progressToken"] == 7 for e in progress)
    assert events[-1]["id"] == 8
    base = {"Authorization": f"Bearer {token}"}
    plain = test_client.post(
        "/api/mcp", headers=base, json=_mcp_ask_body(progress_token="pt-x")
    )
    assert plain.status_code == 200
    assert plain.headers["content-type"].startswith("application/json")
    assert plain.json()["id"] == 8
    assert plain.json()["result"]["structuredContent"]["answer"] == "藏知是个人知识中枢。"

    no_token = test_client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        json=_mcp_ask_body(),
    )
    assert no_token.headers["content-type"].startswith("application/json")
    assert no_token.json()["result"]["isError"] is False

    no_accept = test_client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json=_mcp_ask_body(progress_token="pt-y"),
    )
    assert no_accept.headers["content-type"].startswith("application/json")
    assert no_accept.json()["id"] == 8

    boolean_token = test_client.post(
        "/api/mcp",
        headers={**base, "Accept": "text/event-stream"},
        json=_mcp_ask_body(progress_token=False),
    )
    assert boolean_token.headers["content-type"].startswith("application/json")


def test_mcp_knowledge_ask_sse_preserves_tool_error(client, monkeypatch):
    test_client, token = _setup_mcp_ask_env(client, monkeypatch)
    with test_client.stream(
        "POST",
        "/api/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json=_mcp_ask_body(progress_token="pt-err", extra={"mode": "deep"}),
    ) as response:
        response.read()
        events = _parse_sse(response.text)
    assert len(events) == 1
    final = events[0]
    assert final["id"] == 8
    assert final["result"]["isError"] is True
    assert final["result"]["structuredContent"]["error"]["code"] == "invalid_arguments"


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
                {
                    "status": "sufficient",
                    "summary": "证据链完整",
                    "missing_evidence": [],
                    "next_query": "",
                },
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
