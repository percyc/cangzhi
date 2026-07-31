# Cangzhi v1 API

Send `Authorization: Bearer <PAT>` and `Accept: application/json`.

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
  "document_ids": []
}
```

Only send one of `scope_slug` and `scope_id`. Request-time filters narrow a
saved scope by intersection; they never broaden it.

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

Supported tools:

- `knowledge_list_scopes`
- `knowledge_list_facets`
- `knowledge_search`
- `knowledge_ask`（需要 `knowledge:ask`；复用藏知问答、精确表格计算和引用）
- `knowledge_get_document`
- `knowledge_get_chunk`

Tool results include both text content and `structuredContent`. Prefer the
structured result.
