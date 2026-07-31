# 藏知外部接入

藏知提供三层稳定只读入口，均使用同一套知识范围、检索与引用逻辑：

1. REST API：`/api/v1`
2. CLI：`python -m apps.cli`
3. MCP Streamable HTTP：`/api/mcp`

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
大模型。只有用户明确要求使用藏知所配置模型时，才调用
`/api/v1/knowledge/ask`。
