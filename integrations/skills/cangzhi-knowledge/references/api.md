# Cangzhi v1 API

Send `Authorization: Bearer <PAT>`, `Accept: application/json`, and
`X-Cangzhi-Workspace: <workspace-slug>`. If the workspace header is omitted,
Cangzhi uses `default`; a workspace-bound PAT always uses its bound workspace.
Scope keys are request-time document grouping labels, not HTTP credentials.

## Read endpoints

- `GET /api/v1/capabilities`
- `GET /api/v1/knowledge/scopes`
- `GET /api/v1/knowledge/facets`
- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/ask`
- `GET /api/v1/knowledge/documents/{id}`
- `GET /api/v1/knowledge/chunks/{id}`

Search body:

```json
{
  "query": "检索问题",
  "limit": 8,
  "scope_slug": "all",
  "category_ids": [],
  "tag_ids": [],
  "source_types": [],
  "connector_ids": [],
  "document_selection": {
    "scope_keys": ["id-a", "id-b"],
    "document_ids": [12, 13]
  }
}
```

Only send one of `scope_slug` and `scope_id`. Request-time filters narrow a
saved scope by intersection; they never broaden it.

Direct REST clients may send `mode: "quick"` (default) or `mode: "deep"`.
This skill must use quick mode only: its host AI is already the analysis
orchestrator and should combine search/read/dataset endpoints itself.

Within `document_selection`, documents matching any scope key are unioned with
the explicit document IDs. Other filters intersect that candidate set. Omit
the object to use the full PAT workspace; an explicitly empty object is an
error and must never fall back to the full workspace.

Use `facets` to discover valid category, tag, source-type, and connector IDs
before adding request-time filters. Options in the same dimension use OR
semantics; different dimensions use AND semantics.

## Stable errors

REST errors use:

```json
{"detail": {"code": "scope_not_found", "message": "知识范围不存在"}}
```

Handle these codes without parsing the message:

- `unauthenticated`: token missing, invalid, expired, or revoked.
- `insufficient_scope`: token lacks the endpoint permission.
- `scope_not_found`: selected saved scope no longer exists.
- `invalid_scope` or `invalid_scope_filter`: selector is invalid.
- `document_not_found` or `chunk_not_found`: content is deleted or no longer
  current.
- `provider_not_configured` or `provider_failed`: Cangzhi answering model is
  unavailable; fall back to search and answer in the current agent.

## MCP

Streamable HTTP endpoint: `POST /api/mcp`.

This server does not expose the legacy `GET /sse` + message-POST transport.
`GET /api/mcp` returns `405 Method Not Allowed`; clients must select
`Streamable HTTP`. If a platform only supports legacy SSE, use the REST endpoint
`POST /api/v1/knowledge/ask` for Cangzhi-generated answers instead.

For self-hosted machine-to-machine clients, prefer the API listener directly
(for example `http://host:8000/api/mcp`) instead of routing a long-lived MCP
response through the Next.js port 3000. Keep the same-origin URL for deployments
where a dedicated reverse proxy is the only public entry point.

Supported tools:

- `knowledge_list_scopes`
- `knowledge_list_facets`
- `knowledge_search`
- `knowledge_ask`（需要 `knowledge:ask`；复用藏知问答、精确表格计算和引用）
- `knowledge_get_document`
- `knowledge_get_chunk`

Tool results include both text content and `structuredContent`. Prefer the
structured result.
`knowledge_ask` is quick-only over MCP. Complex work should be planned by the
external agent with the retrieval and dataset tools; do not nest a second deep
agent inside the MCP call.
Clients can request coarse-grained progress for `knowledge_ask` by including a
string or integer `params._meta.progressToken` and accepting `text/event-stream`.
The server emits MCP `notifications/progress` messages and finishes the same SSE
response with the normal JSON-RPC tool result. Without both signals, the endpoint
keeps returning one JSON response for compatibility. Progress messages expose
execution stages only, never chain-of-thought or token-by-token model output.
`knowledge_get_document` returns a bounded content window (12000 characters by
default). Follow `content_window.next_offset` only when more source text is
actually required; never request an entire large spreadsheet merely to answer
a filter or aggregation question.
