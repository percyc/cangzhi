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
- `GET /api/v1/knowledge/documents/{document_id}/enhancements`
- `GET /api/v1/knowledge/enhancements/{run_id}`

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
- `enhancement_not_found`: run/window is missing or outside the permitted boundary.
- `enhancement_stale`: source version or structure changed. Rediscover current
  runs; do not silently reuse old interpretations as current evidence.
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
- `knowledge_get_document_map`
- `knowledge_get_document_block`
- `knowledge_get_chunk`
- `knowledge_list_enhancements`
- `knowledge_get_enhancement`
- `knowledge_get_enhancement_overview`

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

## Source navigation without enhancement

Capability `source_navigation`, scope `knowledge:read`:

- GET `/api/v1/knowledge/documents/{id}/map`, MCP `knowledge_get_document_map`:
  `view=outline|blocks` (default outline), `offset=0`, `limit=20` (max100).
  `items[].id` is a version/fingerprint-bound block ID. Headings are parser output,
  not inferred chapters; blocks includes headings and empty/repeated source blocks.
- GET `/api/v1/knowledge/documents/{id}/block`, MCP `knowledge_get_document_block`:
  required `block_id`, `offset=0`, `max_chars=4000` (max12000). Exact source text is
  in `block.text`; `block.next_offset` and `block.partial` describe character windows.
- REST selection parameters and MCP `document_selection` intersect the grant boundary.
  HTTP409 `source_changed` requires a new map; HTTP409 `structure_unavailable` and
  HTTP413 `structure_too_large` require bounded document reads or dataset tools.
  No automatic reparse, job, or model call. Heading titles/path labels may be
  explicitly truncated; read the original block for exact title text.

```bash
python -m apps.cli document-map 42 --view blocks --limit 20
python -m apps.cli document-block 42 --block-id '<id from map>' --max-chars 4000
```

## Optional enhancement exploration (no extra model calls)

Requires `knowledge:read` and `capabilities.features.enhancement_read`.
List runs for one document (`limit=20`, max50, `offset=0`); each item has version,
fingerprint, status and coverage. No result may mean enhancement was never enabled.
Get one window via `window_index=0`, `view=summary|entities|relations|events|evidence`.
Views use `offset=0`, `limit=5` (max20), return `items`, `total`, `next_offset`, and
`next_window_index`. Original segments are only in the `evidence` view. A pending
window is not an analyzed empty window. Preserve run/version/window and original
block/character/page locators when citing. Analysis is explicitly
`model_extracted_unverified`, not a substitute for the underlying evidence.

REST requests may repeat `scope_keys` / `document_ids` query parameters; MCP uses
the existing `document_selection` object. Each selector intersects the temporary
exploration credential's server-side boundary. No selection can widen that boundary.
These tools cannot start, resume, re-index or otherwise generate enhancement work.

CLI examples (no credentials in arguments):

```bash
python -m apps.cli enhancements 42
python -m apps.cli enhancement 7 --window-index 0 --view summary
python -m apps.cli enhancement 7 --window-index 0 --view evidence --limit 5
python -m apps.cli enhancement-overview 7 --node-key L1:0
```

For a built hierarchy, GET `/api/v1/knowledge/enhancements/{run_id}/overview`
(MCP `knowledge_get_enhancement_overview`) defaults to the root. Pass `node_key`
to read a specific node; each node has at most four children.
`node.result.summary.support_refs` identifies children by `ref`.
Follow `n:` references using the child's `node_key`; follow `w:` references with
`knowledge_get_enhancement(window_index=...,view=evidence)` to read original text.
`enabled=false,status=not_enabled` means this run did not request overview.
Pending/failed nodes have no completed result. Current source/version and scope
checks match window reads. Node `entity_candidates` preserve window/local IDs;
the list is capped at20 with total/truncated and is explicitly unresolved.

Search responses may include `hits[].supporting_evidence` (at most one item).
Read its `context` as well as the primary hit; preserve its own `document_id`,
`document_version_id`, and `chunk.id` when reading or citing it. These are
separately located source windows, not contiguous text. Each context is capped
at1800 characters; no extra model call is made to return this evidence. The
primary search hit and document ranking remain unchanged. Older servers may
omit the field. An empty array does not prove there are no other relevant chunks.
