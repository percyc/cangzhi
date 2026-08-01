# 藏知外部接入

藏知不仅提供自己的搜索与问答页面，也可以作为外部 AI 平台的个人证据中枢。
外部 Agent 默认负责理解用户意图和组织回答，藏知负责返回可追溯的知识片段。

藏知提供三层稳定只读入口，均使用同一套知识范围、混合检索与引用逻辑：

1. REST API：`/api/v1`
2. CLI：`python -m apps.cli`
3. MCP Streamable HTTP：`/api/mcp`

REST 的 `/api/v1/knowledge/facets` 与 MCP 的 `knowledge_list_facets`
用于发现可用分类、标签、来源类型和 WebDAV 连接器。检索时同一维度内满足任一
选项，不同维度之间同时满足；临时筛选只能继续缩小已有 KnowledgeScope。

这三种入口不会绕过藏知的回收站、KnowledgeScope 或文档存活状态，也不会各自维护
另一套向量索引。

## 创建凭证

使用浏览器登录藏知后调用 `POST /api/access-tokens` 创建个人访问令牌。
令牌明文只返回一次，数据库仅保存 SHA-256。建议外部平台只授予
`knowledge:read` 和 `knowledge:search`；只有确实需要调用藏知问答模型时，
才授予 `knowledge:ask`。

## Hermes / OpenClaw

可复制 `integrations/skills/cangzhi-knowledge` 到平台支持的 Skills 目录。
配置两个环境变量：

```text
CANGZHI_URL=http://192.168.50.136:8000
CANGZHI_TOKEN=cz_pat_...
```

若平台原生支持远程 MCP，连接 `${CANGZHI_URL}/api/mcp`，并在 HTTP 请求
头中配置：

```text
Authorization: Bearer ${CANGZHI_TOKEN}
```

默认工作流是“藏知提供证据、外部 Agent 负责回答”，从而避免重复调用两次
大模型。需要藏知直接给出带引用回答，尤其是精确筛选或计算表格时，REST 调用
`/api/v1/knowledge/ask`，MCP 调用 `knowledge_ask`；对应令牌必须包含
`knowledge:ask`。

网页、REST 和 CLI 问答可选择 `quick | deep`，CLI 对应 `cangzhi ask --deep`。
MCP 的 `knowledge_ask` 固定为快速问答，不暴露 `deep`；Skill 和外部 Agent 应自行
组合检索、读取和数据集工具，避免“外部 AI → 藏知 AI → 外部 AI”的重复推理。

`knowledge_get_document` 是分页原文读取工具，默认最多返回 12000 字符，并在
`content_window` 中给出总长度、截断状态和 `next_offset`。大型 Excel 或长文档不要
一次读取全文：表格筛选与统计使用 `knowledge_ask`，证据定位使用
`knowledge_search` / `knowledge_get_chunk`，确需原文时再按窗口逐页读取。

结构化数据也提供独立的发现和执行工具：

- `knowledge_list_datasets`：发现当前有效数据集；
- `knowledge_get_dataset_schema`：读取字段类型、画像与样例；
- `knowledge_preview_dataset_rows`：只分页预览少量行；
- `knowledge_query_dataset`：用白名单计划执行筛选、投影、排序、分组和聚合。

外部 Agent 应先读 schema 再生成计划。`knowledge_query_dataset` 不接收 SQL，查询由
DuckDB 在 Parquet 上下推执行，最多返回 200 行。不要循环调用预览工具把大型 Excel
全部塞进模型上下文；需要完整导出时应使用未来的异步导出接口，而不是 MCP 对话。

## 权限边界

- `knowledge:read`：读取知识范围、文档和切片。
- `knowledge:search`：执行知识检索。
- `knowledge:ask`：调用藏知配置的对话模型生成带引用回答。

读取和检索是推荐的默认权限。首版外部接口不开放采集、修改、删除或令牌管理；
未来写入能力将使用独立的 `knowledge:write` 权限和幂等任务接口。
