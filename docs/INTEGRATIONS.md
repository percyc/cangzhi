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

## 权限边界

- `knowledge:read`：读取知识范围、文档和切片。
- `knowledge:search`：执行知识检索。
- `knowledge:ask`：调用藏知配置的对话模型生成带引用回答。

读取和检索是推荐的默认权限。首版外部接口不开放采集、修改、删除或令牌管理；
未来写入能力将使用独立的 `knowledge:write` 权限和幂等任务接口。
