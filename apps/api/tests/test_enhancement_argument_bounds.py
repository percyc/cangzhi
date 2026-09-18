"""Public argument bounds must fail before invoking the read service."""
import pytest

from test_enhancement_adapters import _live_services, _mcp_call, _owner, _token


@pytest.mark.parametrize("path", [
    "/api/v1/knowledge/documents/1/enhancements",
    "/api/v1/knowledge/enhancements/1",
])
def test_rest_selection_bounds(client, monkeypatch, path):
    test_client, _ = client
    _owner(test_client)
    token = _token(test_client, "bounds", ["knowledge:read"])
    calls = _live_services(monkeypatch)
    for params, status in [
        ([("scope_keys", "")], 400),
        ([("document_ids", "0")], 400),
        ([("scope_keys", f"example-{i}") for i in range(101)], 422),
        ([("document_ids", str(i + 1)) for i in range(201)], 422),
    ]:
        response = test_client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == status
    assert calls == []


@pytest.mark.parametrize("name,args", [
    ("knowledge_list_enhancements", {"document_id": True}),
    ("knowledge_get_enhancement", {"run_id": True}),
    ("knowledge_get_enhancement", {"run_id": 1, "window_index": False}),
    ("knowledge_get_enhancement", {"run_id": 1, "limit": True}),
])
def test_mcp_rejects_boolean_integer_arguments(client, monkeypatch, name, args):
    test_client, _ = client
    _owner(test_client)
    token = _token(test_client, "bounds", ["knowledge:read"])
    calls = _live_services(monkeypatch)
    result = _mcp_call(client, "tools/call", {"name": name, "arguments": args}, token).json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_arguments"
    assert calls == []
