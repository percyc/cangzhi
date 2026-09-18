# 藏知 API、MCP 与 CLI 使用参考

本文档面向要接入藏知的开发者和 AI Agent，提供三层外部入口的完整使用说明：

1. **REST API**：`/api/v1`（稳定知识读取与受控文档写入接口）
2. **CLI**：`python -m apps.cli`
3. **MCP Streamable HTTP**：`/api/mcp`

这三层入口共享同一套知识范围、混合检索与引用逻辑，不会绕过回收站、
KnowledgeScope 或文档存活状态，也不各自维护另一套向量索引。

> 本文档是「怎么调用」的速查手册；产品逻辑见 [PRODUCT.md](PRODUCT.md)，
> 架构与检索/安全边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，
> 外部接入总览见 [INTEGRATIONS.md](INTEGRATIONS.md)。

---

## 1. 认证与工作空间

### 1.1 个人访问令牌（PAT）

用浏览器登录藏知后，调用 `POST /api/access-tokens` 创建令牌。令牌明文只返回一次，
数据库仅保存 SHA-256。每个令牌独立作用域、最小权限、可撤销。

对外部接口，令牌通过 `Authorization: Bearer <token>` 传递。

推荐的最小权限组合：

| 作用域 | 用途 | 建议 |
| --- | --- | --- |
| `knowledge:read` | 读取知识范围、文档、片段、证据 | 默认授予 |
| `knowledge:search` | 执行检索、数据集查询 | 默认授予 |
| `knowledge:ask` | 调用藏知对话模型生成带引用回答 | 仅确需时授予 |
| `documents:write` | 上传文档、整体替换文档 Scope Key | 仅可信外部系统 |

### 1.2 工作空间

所有外部入口默认访问 `default`。访问其他空间时发送请求头：

```text
X-Cangzhi-Workspace: research
```

- CLI：`--workspace research` 或环境变量 `CANGZHI_WORKSPACE=research`
- 远程 MCP：连接配置的 `headers` 中同时放入 `Authorization` 和 `X-Cangzhi-Workspace`
- 新令牌可绑定一个空间；绑定后不能通过请求头或查询参数切换空间
- 未绑定旧令牌保持兼容，由 `X-Cangzhi-Workspace` 选择空间

### 1.3 文档 Scope Key

PAT 决定可访问的工作空间。Scope Key 是不透明的文档分组标识，不是认证信息。Search、
Ask 和 MCP 可通过 `document_selection` 同时指定多个 `scope_keys` 与多个
`document_ids`；两组内部及两组之间均取并集，再与 KnowledgeScope 和元数据筛选取交集。
不传表示全空间，显式空选择返回 `400 invalid_document_selection`。详见
[外部系统接入](EXTERNAL_CLIENT_ACCESS.md)。

### 1.4 通用信息

- 基地址默认 `http://localhost:8000`（局域网可为 `http://192.168.50.136:8000`）
- v1 接口统一前缀 `/api/v1`
- 响应均为 JSON（流式接口除外）
- `/api/v1/capabilities` 会返回当前服务能力，接入前可先探测

### 1.5 MCP 短期探索凭证

现有 `cz_pat_...` 仍可直接调用 `/api/mcp`，并保持工作空间级访问。若第三方系统需要
把一次用户选择固化为外部 AI 不可越过的边界，可由绑定工作空间的 PAT 调用：

- `POST /api/v1/exploration-grants`：请求体包含非空 `document_selection` 和
  `ttl_seconds`（60–86400，默认 3600），返回仅显示一次的 `cz_eg_...`。
- `POST /api/v1/exploration-grants/{grant_id}/revoke`：必须使用创建它的 PAT。

`cz_eg_...` 可作为 `/api/mcp` 或只读 `/api/v1/knowledge/**` 的 Bearer 凭证，也可调用
`/api/v1/capabilities`。它固定工作空间与文档边界；MCP 工具或 REST 请求的筛选只能和
该边界取交集，按 ID 直接读取也不能越界。上传、Scope Key 管理、凭证管理和历史对话
仍被禁止。服务端只保存令牌哈希，创建 PAT 失效时派生凭证同时失效。完整流程见
[外部系统接入](EXTERNAL_CLIENT_ACCESS.md)。

在浏览器打开 `/docs` 可以直接模拟请求：点击右上角 **Authorize**，只填写完整的
`cz_pat_...`（不需要手写 `Bearer`），然后展开 `POST /api/v1/exploration-grants`，点击
**Try it out**。非默认空间同时填写该接口显示的 `X-Cangzhi-Workspace` 参数。创建响应中
复制 `cz_eg_...` 后，可以再次在 Authorize 中替换令牌，调试 `/api/v1/knowledge/**`
或 `/api/mcp`；写入和管理接口会返回 403。

---

## 2. 知识范围（KnowledgeScope）

外部检索与问答都支持用「范围」收窄候选。范围由以下维度组成：

- `scope_id` / `scope_slug`：系统或用户已保存的范围（互斥）
- `category_ids`：分类 ID 列表
- `tag_ids`：标签 ID 列表
- `source_types`：来源类型列表（如 `note`、`web`、`file`）
- `connector_ids`：WebDAV 连接器 ID 列表
- `document_selection`：Scope Key 与明确文档 ID 组成的候选并集

**匹配规则**：同一维度内满足任一选项（OR）；不同维度之间同时满足（AND）。
临时筛选只能继续缩小已有 KnowledgeScope，不能扩大。

发现可用选项：

- REST：`GET /api/v1/knowledge/scopes` 与 `GET /api/v1/knowledge/facets`
- 文档目录：`GET /api/v1/knowledge/documents` 或 MCP `knowledge_list_documents`
- MCP：`knowledge_list_scopes` 与 `knowledge_list_facets`
- CLI：`cangzhi scopes` 与 `cangzhi facets`

---

## 3. REST API v1 参考

除 `capabilities` 外，所有端点都在 `require_workspace_context` 保护下，
并校验令牌作用域。以下每个端点都给出方法、路径、所需作用域、参数与示例。

### 3.1 能力探测

**`GET /api/v1/capabilities`** — 作用域 `knowledge:read`

返回服务能力、支持的认证方式、功能开关和数据执行后端。接入前先调用以确认
版本与能力。

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/capabilities
```

### 3.2 知识范围

**`GET /api/v1/knowledge/scopes`** — 作用域 `knowledge:read`

列出全部、随手记、网页、文件和用户保存的知识范围。

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/knowledge/scopes
```

**`GET /api/v1/knowledge/facets`** — 作用域 `knowledge:read`

列出可用于收窄检索的分类、标签、来源类型和 WebDAV 连接器。

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/knowledge/facets
```

### 3.3 文档上传与 Scope Key 管理

- `POST /api/v1/documents`（`documents:write`）：multipart 上传，支持 `title`、
  `external_id`、`scope_keys`，返回 `202`。
- `GET /api/v1/documents/{id}/processing-status`（`documents:write`）：按上传响应的
  `status_url` 查询解析、AI 整理、切片和向量化进度。
- `GET /api/v1/documents/{id}/scope-keys`（`documents:write`）：读取关联。
- `PUT /api/v1/documents/{id}/scope-keys`（`documents:write`）：整体替换关联。
- `PUT /api/v1/document-scope-keys/batch`（`documents:write`）：按文档 ID 或
  `external_id` 批量整体替换。
- `GET /api/v1/knowledge/documents/{id}/original|preview`（`knowledge:read`）：在令牌
  工作空间内按明确 ID 下载原文或打开转换预览。

上传和管理示例见[外部系统接入](EXTERNAL_CLIENT_ACCESS.md)。

### 3.4 检索

**`POST /api/v1/knowledge/search`** — 作用域 `knowledge:search`

正文关键词 + 向量混合检索，返回命中片段、章节路径和原文定位。

请求体：

```json
{
  "query": "服务可用性预算",
  "limit": 20,
  "offset": 0,
  "scope_slug": "all",
  "category_ids": [],
  "tag_ids": [],
  "source_types": [],
  "connector_ids": [],
  "document_selection": {
    "scope_keys": ["project-a", "project-b"],
    "document_ids": [12, 13]
  }
}
```

字段约束：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `query` | string | 必填，1–512 字符 |
| `limit` | int | 1–50（默认 20） |
| `offset` | int | 0–10000 |
| `scope_id` / `scope_slug` | int / string | 互斥，二选一 |

响应关键字段：`total`、`hits[]`（每个 hit 含 `document_id`、
`document_version_id`、`title`、`source_type`、`score`、`chunk`、
`snippet`、`highlights`、`categories`、`tags`）、`scope`、`retrieval`。

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"服务可用性预算","limit":10}' \
  http://localhost:8000/api/v1/knowledge/search
```

### 3.5 问答（带引用）

**`POST /api/v1/knowledge/ask`** — 作用域 `knowledge:ask`

由藏知配置的对话模型生成带引用回答。支持 `quick` 与 `deep` 两种模式，
`deep` 会执行受控多步检索与数据集精确计算。

请求体：

```json
{
  "question": "UAT 阶段有哪些遗留问题？",
  "mode": "quick",
  "conversation_id": null,
  "context_label": null,
  "scope_slug": "all"
}
```

字段约束：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `question` | string | 必填，1–500 字符 |
| `mode` | string | `quick` \| `deep`，默认 `quick` |
| `conversation_id` | int | 可选，绑定的问答会话 ID |
| `context_label` | string | 可选，最多 512 字符 |

响应关键字段：`question`、`answer`、`insufficient_evidence`、
`citations[]`、`evidence[]`（版本绑定的可取证证据）、`provider`、`model`、
`retrieval`、`scope`。若传了 `conversation_id`，还会返回 `history`。

**`POST /api/v1/knowledge/ask/stream`** — 作用域 `knowledge:ask`

流式版本，返回 `application/x-ndjson`。每个事件一行 JSON，类型为
`progress`、`result` 或 `error`。`progress` 事件用于展示可审计的执行阶段
（检索、分析、回答生成），**不包含模型思维链，也不是逐字 token 输出**。

```bash
curl -N -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"UAT 阶段有哪些遗留问题？","mode":"deep"}' \
  http://localhost:8000/api/v1/knowledge/ask/stream
```

### 3.6 读取文档与片段

**`GET /api/v1/knowledge/documents`** — 作用域 `knowledge:read`

分页列出当前可访问范围内的文档元数据，不返回正文或 Scope Key。可重复传入
`scope_keys` 与 `document_ids`，两类选择器组成候选并集；再与探索凭证的固定边界
取交集。省略选择器时，普通 PAT 列出工作空间文档，探索凭证只列出其边界内文档。

```bash
curl -G -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "scope_keys=id-a" \
  --data-urlencode "scope_keys=id-b" \
  --data-urlencode "document_ids=42" \
  --data-urlencode "limit=50" \
  http://localhost:8000/api/v1/knowledge/documents
```

响应包含 `items`、`total`、`limit`、`offset`。每项包含文档 ID、标题、描述、来源、
外部 ID、更新时间和当前版本处理状态。

**`GET /api/v1/knowledge/documents/{document_id}`** — 作用域 `knowledge:read`

读取当前文档正文。可选 `version_id` 查询参数用于版本绑定；若指定的版本与当前
不一致，返回 `409 version_mismatch`，避免用新版本冒充原始证据。

**`GET /api/v1/knowledge/chunks/{chunk_id}`** — 作用域 `knowledge:read`

读取单个片段及可定位元数据。可选 `version_id`、`include_context`（为 `true` 时
同时返回版本绑定的证据上下文）。

> 大型 Excel 或长文档不要一次读取全文。MCP 的 `knowledge_get_document`
> 会按 `content_window` 分页；REST 的文档读取接口返回结构化正文，长文档
> 建议配合检索与片段接口定位证据后再读取。

### 3.7 结构化数据集

**`GET /api/v1/knowledge/datasets`** — 作用域 `knowledge:read`

列出当前有效文档中的二维数据集。可选 `document_id`（限定文档）、`limit`。

**`GET /api/v1/knowledge/datasets/{dataset_id}/schema`** — 作用域 `knowledge:read`

读取字段类型、语义角色、样例、统计画像和执行后端。规划查询前应先调用。

**`GET /api/v1/knowledge/datasets/{dataset_id}/rows`** — 作用域 `knowledge:read`

分页预览少量行。禁止用它逐页抓取整个大表。

**`POST /api/v1/knowledge/datasets/{dataset_id}/query`** — 作用域 `knowledge:search`

通过受控计划执行筛选、投影、排序、分组和聚合，由 DuckDB/Parquet 下推计算。
**不接受 SQL**，最多返回 200 行。

请求体：

```json
{
  "filters": [
    {"column": "状态", "operator": "eq", "value": "已验收"}
  ],
  "columns": ["编号", "状态"],
  "group_by": [],
  "metric": "rows",
  "metric_column": null,
  "sort_by": "编号",
  "sort_order": "asc",
  "limit": 50,
  "offset": 0
}
```

`filters` 每项用 `column`、`operator`、`value`。`operator` 支持：
`eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`contains`、`starts_with`、
`ends_with`、`direct_child_of`、`in`。`direct_child_of` 用于安全选择层级路径的
直属子级。

查询为零行时，还应读取响应的 `query_hints`；例如层级字段存在直属子级而父级
本身没有记录时，按 `suggested_filter` 改用 `direct_child_of` 后继续验证和聚合，
不应把首次零结果当作最终结论。

响应除 `rows`、`matched_row_count`、`source_rows` 与执行后端外，还包含
`document_id`、`document_version_id`、`title`、`artifact_version` 和服务端规范化
后的 `query_plan`。外部客户端应保存这些字段，并以 `document_version_id +
dataset_id + artifact_version + source_rows` 打开下方版本绑定证据接口；不要只保存
文档标题或把普通数据表预览当作回答证据。

### 3.8 证据取证

以下端点返回版本绑定的证据上下文，防止读取新版本冒充原始证据。

**`GET /api/v1/knowledge/evidence/by-chunk/{chunk_id}`** — 作用域 `knowledge:read`
- 必填查询参数 `document_version_id`
- 返回与原始回答版本一致的章节高亮、PDF/Word 解析段 + 页码跳转、数据集计算结果

**`GET /api/v1/knowledge/evidence/by-dataset/{dataset_id}`** — 作用域 `knowledge:read`
- 必填 `document_version_id`，可选 `artifact_version`

**`POST /api/v1/knowledge/evidence/by-dataset/{dataset_id}/rows`** — 作用域 `knowledge:read`
- 仅返回 `source_rows` 中明确请求的原始行，不返回 SQL，不放任把整表装入上下文
- 请求体：`{"columns": [...], "source_rows": [1,2,3], "limit": 20}`

---

### 3.9 已构建的知识增强产物

先检查 `GET /api/v1/capabilities` 中 `features.enhancement_read`。以下接口要求
`knowledge:read`，普通 PAT、登录 Cookie 和临时探索凭证均可用；**不会调用模型或发起任务**。
没有记录并不表示原文没有相关事实，可能只是尚未开启/构建增强，仍可使用原文检索。

**`GET /api/v1/knowledge/documents/{document_id}/enhancements`**

- `offset=0`、`limit=20`（1–50）；仅列当前文档版本且原文指纹一致的运行。
- 返回 `document_id`、`document_version_id`、`items`、`total`、`next_offset`。
  运行摘要包含 `id`、`status`、`source_fingerprint`、完成窗口/原文字数和调用计数。

**`GET /api/v1/knowledge/enhancements/{run_id}`**

- `window_index=0`（零基）；一次只读取一个窗口。
- `view=summary`，可选 `entities`、`relations`、`events`、`evidence`。
- `offset=0`、`limit=5`（1–20），分页作用于当前窗口的指定视图。
- 返回 `run`、`window_index`、`window_status`、`view`、`items`、`total`、`next_offset`、
  `next_window_index`。已完成但无信息与尚未分析窗口由状态区分。
- `summary` 项含 `text`、`evidence_ids`；图谱项中的实体 ID 及整数证据 ID 均局限在
  当前运行/窗口。按需切换 `view=evidence` 读取对应 `id` 的原文及块/字符/页码定位。
  不同窗口的同名实体不能自动视为同一对象。
- 模型解释携带 `evidence_status=model_extracted_unverified`；锚点有效不是语义正确
  的保证，引用应回到原文。基础切片与增强窗口不是同一个 ID 空间。

两条接口都可用重复查询参数 `scope_keys`、`document_ids` 进一步选择文档，组内及
两组之间按并集，再与当前空间和探索凭证边界取交集，不能通过猜运行 ID 越界。
范围外、回收和不存在对象返回404；源版本/结构变化返回409 `enhancement_stale`，
应重新列出当前有效运行。未开放外部开始、续跑、取消等写操作。

MCP 等价工具为 `knowledge_list_enhancements`、`knowledge_get_enhancement`，选择
参数使用现有 `document_selection` 对象，其他字段与 REST 相同。建议外部 AI 自行
决定继续读取哪些窗口及视图，不批量拉取所有图谱或嵌套调用藏知深度分析。

## 4. MCP Streamable HTTP 参考

接入点：`POST http://localhost:8000/api/mcp`（或经 Next.js 同源代理的
`http://localhost:3000/api/mcp`）。

藏知当前实现的是 MCP **Streamable HTTP**，不是旧版独立 HTTP+SSE 传输：

- 支持：`POST /api/mcp`，普通请求返回 JSON，需要进度时可在同一 POST 响应中返回 SSE。
- 不支持：`GET /api/mcp/sse` + 独立消息 POST 端点。
- `GET /api/mcp` 当前明确返回 `405 Method Not Allowed`。

因此 Dify、Hermes 或其他客户端必须选择 `Streamable HTTP`。如果客户端只有
`SSE` 选项，不能直接连接藏知 MCP；取得藏知深度回答时请改用
`POST /api/v1/knowledge/ask`（或 `/ask/stream`）。局域网 Docker 部署优先使用
`http://主机:8000/api/mcp`，避免把长连接经过 Next.js 的 3000 端口。

- 支持协议版本：`2025-06-18`、`2025-03-26`
- 认证：请求头 `Authorization: Bearer <token>`，可选 `X-Cangzhi-Workspace`
- 支持方法：`initialize`、`ping`、`notifications/initialized`、`tools/list`、
  `tools/call`
- 所有工具只读：`readOnlyHint = true`、`destructiveHint = false`

`initialize` 返回的 `instructions` 字段给出了工具编排指引，建议在接入时读取。

### 4.1 工具清单

| 工具 | 作用域 | 说明 |
| --- | --- | --- |
| `knowledge_list_scopes` | read | 列出知识范围 |
| `knowledge_list_facets` | read | 列出分类/标签/来源/连接器筛选项 |
| `knowledge_list_documents` | read | 分页列出当前范围内的文档元数据，可按 Scope Key/文档 ID 筛选 |
| `knowledge_search` | search | 检索证据片段，适合外部模型自行组织回答 |
| `knowledge_ask` | ask | 快速检索并回答，返回可核验引用；固定快速模式，不暴露 deep |
| `knowledge_get_document` | read | 按文档 ID 分页读取正文（默认最多 12000 字符） |
| `knowledge_get_chunk` | read | 按片段 ID 读取当前有效片段 |
| `knowledge_list_datasets` | read | 列出结构化数据集 |
| `knowledge_get_dataset_schema` | read | 读取字段类型、画像与样例 |
| `knowledge_preview_dataset_rows` | read | 只分页预览少量行 |
| `knowledge_query_dataset` | search | 用白名单计划执行筛选/投影/排序/分组/聚合 |
| `knowledge_get_evidence_by_chunk` | read | 按片段读取版本绑定证据上下文 |
| `knowledge_get_evidence_by_dataset` | read | 按数据集读取版本绑定证据上下文 |
| `knowledge_preview_evidence_rows` | read | 按 `source_rows` 预览贡献行 |

### 4.2 调用示例（JSON-RPC）

列工具：

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

检索：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "knowledge_search",
    "arguments": {"query": "服务可用性预算", "limit": 10, "scope_slug": "all"}
  }
}
```

完整 curl：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"tools/call",
    "params":{"name":"knowledge_search","arguments":{"query":"服务可用性预算","limit":10}}
  }' \
  http://localhost:8000/api/mcp
```

### 4.3 `knowledge_ask` 进度通知

`knowledge_ask` 支持 MCP 进度通知。支持该能力的客户端在 `tools/call.params._meta`
中传入字符串或整数型 `progressToken`，并声明 `Accept: text/event-stream`。
藏知随后通过 `notifications/progress` 依次报告检索、分析和回答生成进度，
最后在同一 SSE 响应中返回标准 JSON-RPC 工具结果。没有请求进度的旧客户端仍收到
单个 JSON 响应。

### 4.4 工具编排建议

`initialize` 返回的 `instructions` 原文如下，接入时建议遵循：

1. 范围不明确时先使用 `knowledge_list_scopes`；需要按分类、标签、来源或
   连接器收窄时使用 `knowledge_list_facets`。
2. 需要藏知直接回答或精确查询表格时使用 `knowledge_ask`；需要原始证据供外部
   模型自行分析时使用 `knowledge_search`。
3. 需要完整上下文时，再按返回的 `chunk.id` 读取。
4. 完整文档必须通过 `knowledge_get_document` 按 `content_window.next_offset`
   分页读取，避免大型文档挤占上下文。
5. 发现表格后先用 `knowledge_list_datasets` 和 `knowledge_get_dataset_schema`，
   再用 `knowledge_query_dataset` 做筛选聚合；**不要通过预览工具遍历整个数据集**。
6. 复杂分析应由当前外部 Agent 组合上述工具完成；MCP 不提供藏知内部 deep 编排，
   以避免双重 Agent 和重复模型消耗。

---

## 5. CLI 参考

CLI 是 `apps/cli` 目录下的一个轻量 Python 客户端，通过环境变量或命令行参数
直连 REST API。标准输出始终为 JSON。

### 5.1 环境变量

| 变量 | 说明 |
| --- | --- |
| `CANGZHI_URL` | API 基地址，默认 `http://localhost:8000` |
| `CANGZHI_TOKEN` | 个人访问令牌（必填） |
| `CANGZHI_WORKSPACE` | 工作空间 slug，默认 `default` |

### 5.2 命令

```bash
python -m apps.cli capabilities                          # 服务能力
python -m apps.cli scopes                                # 列出知识范围
python -m apps.cli facets                                # 列出筛选项
python -m apps.cli search "关键词" --limit 20            # 检索
python -m apps.cli ask "问题" [--deep]                   # 带引用问答
python -m apps.cli ask "问题" --deep                     # 深度多步检索
python -m apps.cli document 12                           # 读取文档
python -m apps.cli chunk 34                              # 读取片段
python -m apps.cli enhancements 12                       # 列出文档增强记录
python -m apps.cli enhancement 7 --window-index 0 --view summary
python -m apps.cli enhancement 7 --window-index 0 --view evidence --limit 5
```

通用选项：

- `--url`、`--token`、`--workspace`、`--timeout`、`--compact`（单行 JSON）

增强命令另外支持 `--offset`、`--limit`、`--scope-keys`、`--document-ids`；
`enhancements` 的位置参数是文档 ID，`enhancement` 的位置参数是增强运行 ID。

检索/问答的范围选项：

- `--scope <slug>` 或 `--scope-id <id>`（互斥）
- `--category-ids`、`--tag-ids`、`--source-types`、`--connector-ids`、
  `--scope-keys`、`--document-ids`（逗号分隔）

示例：

```bash
export CANGZHI_URL=http://192.168.50.136:8000
export CANGZHI_TOKEN=cz_pat_xxxx

python -m apps.cli search "服务可用性预算" --category-ids 3,5 --limit 10
python -m apps.cli ask "UAT 阶段有哪些遗留问题？" --deep
python -m apps.cli ask "统计各状态缺陷数量" --scope project_x
```

---

## 6. 权限边界与错误处理

### 6.1 权限边界

- `knowledge:read`：读取知识范围、文档和切片（含证据）。
- `knowledge:search`：执行知识检索与数据集查询。
- `knowledge:ask`：调用藏知配置的对话模型生成带引用回答。
- `documents:write`：上传文档和管理文档 Scope Key，不包含删除或任意 SQL。

读取和检索是推荐的默认权限。文档上传和 Scope Key 管理已经通过独立
`documents:write` 开放；删除、令牌管理和任意 SQL 仍不对外开放。

### 6.2 错误约定

- REST：错误返回 `{"detail": {"code": "...", "message": "..."}}`。
  常见状态码：`400`（参数/范围错误）、`404`（范围/文档/片段/数据集不存在）、
  `408`（数据集查询超时）、`409`（版本不匹配，需避免用新版本冒充证据）、
  `502`（模型调用失败）、`503`（模型未配置或不可用）。
- MCP：工具错误返回 `isError: true` 的 `_tool_error`，其 `structuredContent`
  含 `error.code` 与 `error.message`。
- CLI：错误写入 stderr，返回非零退出码，`stdout` 始终为纯 JSON。

### 6.3 版本绑定（取证关键）

`document_version_id` 是证据链的核心：问答产出的每条 `evidence` 都携带生成时的
版本号。读取证据或原文时必须传入与回答**完全相同**的 `document_version_id`；
一旦文档被重新处理后版本变化，旧版本号会触发 `409 version_mismatch`，防止
拿新版本内容冒充历史回答的依据。

---

## 7. 典型使用场景

### 7.1 外部 Agent 自行检索并回答（推荐）

1. `knowledge_list_scopes` / `knowledge_list_facets` 确定知识边界。
2. `knowledge_search` 检索候选片段。
3. 按需 `knowledge_get_chunk` 读取完整片段。
4. 外部模型结合片段组织回答，引用 `document_id` 与 `chunk.id`。

适合 Hermes、OpenClaw 等，避免外部 AI → 藏知 AI → 外部 AI 的重复推理。

### 7.2 精确查询/计算表格

1. `knowledge_list_datasets` 发现数据集。
2. `knowledge_get_dataset_schema` 读字段类型与画像。
3. `knowledge_query_dataset` 执行筛选、分组、聚合（DuckDB/Parquet 下推）。
4. 零结果时读取 `query_hints` 并按 `suggested_filter` 调整后继续。

### 7.3 审计取证

对一次问答结果，用其 `evidence[].document_version_id` 调用
`knowledge_get_evidence_by_chunk` / `knowledge_get_evidence_by_dataset`，
再按 `source_rows` 用 `knowledge_preview_evidence_rows` 读取贡献原始行，
即可核对模型回答的原始依据。

### 7.4 云盘/长文档

- 定位证据：`knowledge_search` + `knowledge_get_chunk`。
- 逐页读原文：`knowledge_get_document`，按 `content_window.next_offset` 翻页。
- 不要用 `knowledge_preview_dataset_rows` 抓取整张表。

---

## 8. 快速接入清单

1. 登录藏知，创建令牌：`POST /api/access-tokens`（授予 `knowledge:read`、
   `knowledge:search`，必要时加 `knowledge:ask`）。
2. 设置环境变量 `CANGZHI_URL`、`CANGZHI_TOKEN`、`CANGZHI_WORKSPACE`。
3. 调 `GET /api/v1/capabilities` 确认能力。
4. 浏览 `scopes` / `facets` 确定知识边界。
5. 按场景选择 REST / CLI / MCP 发起检索或问答。
6. 需要带引用回答时授予 `knowledge:ask` 并调用 `ask`（REST/CLI）或
   `knowledge_ask`（MCP）。

## 9. 浏览器管理接口：收件箱

`GET /api/documents/inbox?filter=attention&offset=0&limit=25` 使用管理员登录会话，
并沿用当前工作空间解析规则；不是面向外部 Agent 的 REST v1/MCP 接口。

- `filter`：`attention`（默认）、`all`、`processing`、`failed`、
  `needs_organization`、`source_issue`、`not_vectorized`、`completed`。
- `offset`：从 0 起；`limit`：默认 25，范围 1–100。
- 响应：`items` 为当前页，`total` 为当前筛选总数，`counts` 为当前空间各筛选
  总数，`has_processing` 表示空间内是否仍有处理中的资料。
- 状态与文档详情共用计算口径；收件箱省略 OCR 页级摘要，不返回原文或向量。
  各筛选可能重叠，例如已完成但待整理的资料仍在“需要关注”中。
- 页面上的批量重试和向量补建仅作用于本页；翻页后可继续处理下一页。
