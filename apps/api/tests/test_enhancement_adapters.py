"""Adapter-level tests for read-only enhancement exploration (HTTP + MCP).

The underlying ``enhancement_read`` service is isolated via monkeypatch so
these tests prove the REST v1 and MCP adapters forward document selection,
exploration boundaries, view/pagination and error mapping without calling a
model or touching the serving index.
"""

import asyncio

import pytest

from apps.api.api import mcp as mcp_module
from apps.api.api import v1 as v1_module
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.models.documents import Document, DocumentSourceType
from apps.api.services.knowledge_read import KnowledgeReadError
from test_knowledge_v1 import _enable_real_login, _setup_owner

VIEWS = ["summary", "entities", "relations", "events", "evidence"]


def _owner(test_client):
    _enable_real_login()
    _setup_owner(test_client)


def _token(test_client, name: str, scopes: list[str], workspace_id=None) -> str:
    payload = {"name": name, "scopes": scopes}
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    created = test_client.post("/api/access-tokens", json=payload)
    assert created.status_code == 201
    return created.json()["token"]


def _seed_scope_document(db_repo, workspace_id, name="kb-x", title="in-scope doc"):
    """Seed a plain document + scope key and return its id."""
    store = {}

    async def seed():
        async for db in db_repo():
            doc = Document(title=title, source_type=DocumentSourceType.note, is_deleted=False)
            db.add(doc)
            await db.flush()
            db.add(DocumentScopeKey(workspace_id=workspace_id, document_id=doc.id, scope_key=name))
            await db.commit()
            store["doc_id"] = doc.id

    asyncio.run(seed())
    return store["doc_id"]


def _live_services(monkeypatch, *, list_payload=None, read_payload=None):
    calls = []

    async def fake_list(db, document_id, *, limit, offset, document_selection, document_boundary):
        calls.append(
            {
                "kind": "list",
                "document_id": document_id,
                "run_id": None,
                "limit": limit,
                "offset": offset,
                "view": None,
                "selection_scope": list(document_selection.scope_keys) if document_selection else None,
                "selection_ids": list(document_selection.document_ids) if document_selection else None,
                "document_boundary": document_boundary,
            }
        )
        return list_payload if list_payload is not None else {"items": [], "total": 0}

    async def fake_read(db, run_id, *, window_index, view, offset, limit, document_selection, document_boundary):
        calls.append(
            {
                "kind": "read",
                "document_id": None,
                "run_id": run_id,
                "window_index": window_index,
                "view": view,
                "offset": offset,
                "limit": limit,
                "selection_scope": list(document_selection.scope_keys) if document_selection else None,
                "selection_ids": list(document_selection.document_ids) if document_selection else None,
                "document_boundary": document_boundary,
            }
        )
        return (
            read_payload
            if read_payload is not None
            else {
                "run": {"id": run_id, "status": "completed"},
                "window": {"window_index": window_index, "view": view},
                "total": 0,
                "next_offset": None,
            }
        )

    monkeypatch.setattr(v1_module, "list_document_enhancements", fake_list)
    monkeypatch.setattr(v1_module, "read_enhancement", fake_read)
    monkeypatch.setattr(mcp_module, "list_document_enhancements", fake_list)
    monkeypatch.setattr(mcp_module, "read_enhancement", fake_read)
    return calls


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/knowledge/documents/11/enhancements",
        "/api/v1/knowledge/enhancements/7",
    ],
)
def test_enhancement_read_requires_knowledge_read_scope(client, monkeypatch, path):
    test_client, _ = client
    _owner(test_client)
    _live_services(monkeypatch)
    read_only = _token(test_client, "read", ["knowledge:read"])
    search_only = _token(test_client, "search", ["knowledge:search"])

    denied = test_client.get(
        path, headers={"Authorization": f"Bearer {search_only}"}
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "insufficient_scope"

    allowed = test_client.get(
        path, headers={"Authorization": f"Bearer {read_only}"}
    )
    assert allowed.status_code == 200


def test_list_endpoint_forwards_selection_and_defaults(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    token = _token(test_client, "list", ["knowledge:read"])
    headers = {"Authorization": f"Bearer {token}"}
    captured = _live_services(monkeypatch)

    response = test_client.get(
        "/api/v1/knowledge/documents/42/enhancements",
        headers=headers,
        params={"scope_keys": "kb-x", "document_ids": 99},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0

    call = [_c for _c in captured if _c["kind"] == "list"][0]
    assert call["document_id"] == 42
    assert call["limit"] == 20
    assert call["offset"] == 0
    assert call["selection_scope"] == ["kb-x"]
    assert call["selection_ids"] == [99]
    assert call["document_boundary"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
    ],
)
def test_list_endpoint_schema_422(client, monkeypatch, params):
    test_client, _ = client
    _owner(test_client)
    _live_services(monkeypatch)
    token = _token(test_client, "schema", ["knowledge:read"])

    response = test_client.get(
        "/api/v1/knowledge/documents/42/enhancements",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    assert response.status_code == 422


@pytest.mark.parametrize("view", VIEWS)
def test_get_endpoint_forward_all_views(client, monkeypatch, view):
    test_client, _ = client
    _owner(test_client)
    captured = _live_services(monkeypatch)
    token = _token(test_client, f"view-{view}", ["knowledge:read"])

    response = test_client.get(
        "/api/v1/knowledge/enhancements/7",
        headers={"Authorization": f"Bearer {token}"},
        params={"window_index": 2, "view": view, "offset": 5, "limit": 10},
    )
    assert response.status_code == 200

    call = [_c for _c in captured if _c["kind"] == "read"][0]
    assert call["run_id"] == 7
    assert call["view"] == view
    assert call["window_index"] == 2
    assert call["limit"] == 10
    assert call["offset"] == 5


def test_get_endpoint_schema_422_invalid_view(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    _live_services(monkeypatch)
    token = _token(test_client, "badview", ["knowledge:read"])

    response = test_client.get(
        "/api/v1/knowledge/enhancements/7",
        headers={"Authorization": f"Bearer {token}"},
        params={"view": "summry"},
    )
    assert response.status_code == 422


def test_get_endpoint_schema_422_invalid_limits(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    _live_services(monkeypatch)
    token = _token(test_client, "badlimits", ["knowledge:read"])

    for params in ({"limit": 0}, {"limit": 21}, {"offset": -2}, {"window_index": -1}):
        response = test_client.get(
            "/api/v1/knowledge/enhancements/7",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        assert response.status_code == 422


def test_boundary_and_selection_forwarded_to_service(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    captured = _live_services(monkeypatch)
    workspace = test_client.get("/api/workspaces/current").json()
    token = _token(test_client, "grant", ["knowledge:read"], workspace_id=workspace["id"])
    repo = app.dependency_overrides[get_db]
    doc_id = _seed_scope_document(repo, workspace["id"], "kb-x")

    grant = test_client.post(
        "/api/v1/exploration-grants",
        json={"document_selection": {"scope_keys": ["kb-x"]}, "ttl_seconds": 600},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert grant.status_code == 201
    grant_token = grant.json()["token"]

    outside = _seed_scope_document(repo, workspace["id"], "kb-other", "outside doc")

    response = test_client.get(
        f"/api/v1/knowledge/documents/{doc_id}/enhancements",
        headers={"Authorization": f"Bearer {grant_token}"},
        params={"document_ids": outside},
    )
    assert response.status_code == 200

    call = [_c for _c in captured if _c["kind"] == "list"][0]
    assert call["document_boundary"] is not None
    boundary_scope = set(call["document_boundary"].scope_keys)
    assert "kb-x" in boundary_scope
    assert "kb-other" not in boundary_scope
    assert call["selection_ids"] == [outside]


def test_error_mapping_to_http(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    monkeypatch.setattr(v1_module, "list_document_enhancements", _raising("document_not_found"))
    monkeypatch.setattr(v1_module, "read_enhancement", _raising("run_not_found"))
    monkeypatch.setattr(mcp_module, "list_document_enhancements", _raising("document_not_found"))
    monkeypatch.setattr(mcp_module, "read_enhancement", _raising("run_not_found"))
    token = _token(test_client, "err", ["knowledge:read"])
    headers = {"Authorization": f"Bearer {token}"}

    assert test_client.get(
        "/api/v1/knowledge/documents/1/enhancements", headers=headers
    ).status_code == 404
    assert test_client.get(
        "/api/v1/knowledge/enhancements/1", headers=headers
    ).status_code == 404

    monkeypatch.setattr(v1_module, "list_document_enhancements", _raising("enhancement_stale"))
    assert test_client.get(
        "/api/v1/knowledge/documents/1/enhancements", headers=headers
    ).status_code == 409
    monkeypatch.setattr(v1_module, "list_document_enhancements", _raising("version_mismatch"))
    assert test_client.get(
        "/api/v1/knowledge/documents/1/enhancements", headers=headers
    ).status_code == 409
    monkeypatch.setattr(v1_module, "list_document_enhancements", _raising("invalid_window"))
    assert test_client.get(
        "/api/v1/knowledge/documents/1/enhancements", headers=headers
    ).status_code == 400


def _raising(code):
    async def fake(*args, **kwargs):
        raise KnowledgeReadError(code=code, message=f"boom {code}")

    return fake


def _mcp_call(client_fixture, method, params, token):
    test_client, _ = client_fixture
    return test_client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def test_mcp_tools_list_exposes_enhancement_tools(client):
    test_client, _ = client
    _owner(test_client)
    token = _token(test_client, "mcp-tools", ["knowledge:read"])

    response = _mcp_call(client, "tools/list", {}, token)
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "knowledge_list_enhancements" in names
    assert "knowledge_get_enhancement" in names


@pytest.mark.parametrize("view", VIEWS)
def test_mcp_get_enhancement_forwards_view(client, monkeypatch, view):
    test_client, _ = client
    _owner(test_client)
    captured = _live_services(monkeypatch)
    token = _token(test_client, f"mcp-view-{view}", ["knowledge:read"])
    arguments = {"run_id": 7, "view": view}
    if view == "summary":
        arguments = {"run_id": 7}

    response = _mcp_call(client, "tools/call", {"name": "knowledge_get_enhancement", "arguments": arguments}, token)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    call = [_c for _c in captured if _c["kind"] == "read"][-1]
    assert call["run_id"] == 7
    assert call["view"] == view
    assert call["limit"] == 5
    assert call["offset"] == 0


def test_mcp_list_enhancement_selection_string_compat(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    captured = _live_services(monkeypatch)
    token = _token(test_client, "mcp-sel", ["knowledge:read"])

    response = _mcp_call(
        client,
        "tools/call",
        {
            "name": "knowledge_list_enhancements",
            "arguments": {"document_id": 3, "document_selection": '{"scope_keys": ["kb-x"]}'},
        },
        token,
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    call = [_c for _c in captured if _c["kind"] == "list"][-1]
    assert call["selection_scope"] == ["kb-x"]


def test_mcp_get_enhancement_error_mapping(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    monkeypatch.setattr(mcp_module, "read_enhancement", _raising("enhancement_stale"))
    monkeypatch.setattr(mcp_module, "list_document_enhancements", _raising("enhancement_stale"))
    token = _token(test_client, "mcp-err", ["knowledge:read"])

    response = _mcp_call(
        client,
        "tools/call",
        {"name": "knowledge_get_enhancement", "arguments": {"run_id": 7}},
        token,
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True


def test_mcp_scope_enforced(client, monkeypatch):
    test_client, _ = client
    _owner(test_client)
    _live_services(monkeypatch)
    token = _token(test_client, "mcp-search", ["knowledge:search"])

    response = _mcp_call(
        client,
        "tools/call",
        {"name": "knowledge_list_enhancements", "arguments": {"document_id": 1}},
        token,
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True