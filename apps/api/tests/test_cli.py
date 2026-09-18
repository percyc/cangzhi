"""Tests for the stable JSON command-line adapter."""

import argparse
import json

import httpx
import pytest
from urllib.parse import urlsplit, parse_qs

from apps.cli.main import CangzhiClient, CLIError, _scope_payload, build_parser, main, run


class StubClient(CangzhiClient):
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, payload=None):
        self.calls.append((method, path, payload))
        return {"ok": True}


def test_search_command_builds_same_scope_contract_as_rest():
    client = StubClient()
    args = argparse.Namespace(
        command="search",
        query="合同期限",
        limit=5,
        offset=2,
        scope="files",
        scope_id=None,
        category_ids="1,2",
        tag_ids=None,
        source_types="file,note",
        connector_ids="3",
        scope_keys=None,
        document_ids=None,
    )
    assert run(args, client) == {"ok": True}
    assert client.calls == [
        (
            "POST",
            "/api/v1/knowledge/search",
            {
                "query": "合同期限",
                "limit": 5,
                "offset": 2,
                "scope_slug": "files",
                "category_ids": [1, 2],
                "source_types": ["file", "note"],
                "connector_ids": [3],
            },
        )
    ]


def test_scope_payload_omits_empty_dimensions():
    args = argparse.Namespace(
        scope=None,
        scope_id=None,
        category_ids="",
        tag_ids=None,
        source_types=None,
        connector_ids=None,
        scope_keys=None,
        document_ids=None,
    )
    assert _scope_payload(args) == {}


def test_facets_command_uses_stable_catalog_endpoint():
    client = StubClient()
    assert run(argparse.Namespace(command="facets"), client) == {"ok": True}
    assert client.calls == [("GET", "/api/v1/knowledge/facets", None)]


def test_ask_command_can_select_deep_analysis():
    client = StubClient()
    args = argparse.Namespace(
        command="ask",
        question="比较两份方案",
        deep=True,
        scope="all",
        scope_id=None,
        category_ids=None,
        tag_ids=None,
        source_types=None,
        connector_ids=None,
        scope_keys=None,
        document_ids=None,
    )

    assert run(args, client) == {"ok": True}
    assert client.calls == [
        (
            "POST",
            "/api/v1/knowledge/ask",
            {
                "question": "比较两份方案",
                "mode": "deep",
                "scope_slug": "all",
            },
        )
    ]


def test_search_command_unions_scope_keys_and_document_ids_in_selection():
    client = StubClient()
    args = argparse.Namespace(
        command="search",
        query="合同期限",
        limit=10,
        offset=0,
        scope=None,
        scope_id=None,
        category_ids=None,
        tag_ids=None,
        source_types=None,
        connector_ids=None,
        scope_keys="tenant-a,tenant-b",
        document_ids="7,9",
    )

    assert run(args, client) == {"ok": True}
    assert client.calls[0][2]["document_selection"] == {
        "scope_keys": ["tenant-a", "tenant-b"],
        "document_ids": [7, 9],
    }


def test_missing_token_is_machine_readable(capsys):
    assert main(["capabilities"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "missing_token"


def test_http_error_preserves_stable_server_code(monkeypatch, capsys):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(
            403,
            json={
                "detail": {
                    "code": "insufficient_scope",
                    "message": "令牌缺少所需权限",
                }
            },
        ),
    )
    assert main(["--token", "cz_pat_test", "capabilities"]) == 3
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "insufficient_scope"


@pytest.mark.parametrize('view', ['summary', 'entities', 'relations', 'events', 'evidence'])
def test_enhancement_cli_routes_and_query_encoding(view):
    client = StubClient()
    args = build_parser().parse_args(['enhancement', '12', '--window-index', '2', '--view', view,
        '--offset', '3', '--limit', '4', '--scope-keys', 'sample & a,示例', '--document-ids', '1,2'])
    run(args, client)
    method, path, payload = client.calls[0]
    assert method == 'GET' and payload is None
    assert urlsplit(path).path == '/api/v1/knowledge/enhancements/12'
    assert parse_qs(urlsplit(path).query) == {'window_index': ['2'], 'view': [view],
        'offset': ['3'], 'limit': ['4'], 'scope_keys': ['sample & a', '示例'], 'document_ids': ['1', '2']}


def test_enhancement_list_cli():
    client = StubClient()
    run(build_parser().parse_args(['enhancements', '7']), client)
    assert client.calls == [('GET', '/api/v1/knowledge/documents/7/enhancements?offset=0&limit=20', None)]


@pytest.mark.parametrize('args', [
    ['enhancements', '1', '--scope-keys', ''], ['enhancements', '1', '--limit', '51'],
    ['enhancement', '1', '--limit', '21'], ['enhancement', '1', '--window-index', '-1'],
    ['enhancements', '0'], ['enhancements', '1', '--document-ids', '-1'],
    ['enhancements', '1', '--offset', '-1']])
def test_enhancement_cli_invalid_requests_do_not_fall_back(args):
    client = StubClient()
    with pytest.raises(CLIError):
        run(build_parser().parse_args(args), client)
    assert client.calls == []
