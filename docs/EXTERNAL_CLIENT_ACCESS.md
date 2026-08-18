# 外部系统接入：上传与文档检索范围

本文描述外部业务系统如何把藏知作为知识中枢。设计决策见
[ADR-021](ADR-021-external-client-access.md)，完整字段以 [API 参考](API_REFERENCE.md) 为准。

## 1. 模型与语义

```text
PAT ──绑定──> Workspace
Document <──多对多──> opaque scope_key（id-a / id-b / ...）
```

- PAT 是工作空间访问凭证；Scope Key 只是外部系统生成和维护的文档分组标识。
- 藏知不登记 Scope Key 用户、角色、授权或 Key 名录。
- 一篇文档可以属于多个 Key。
- 不传 `document_selection`：查询工作空间全部文档。
- 查询时，多个 Scope Key 与多个明确 Document ID 共同组成文档候选并集。
- 空 `scope_keys`：文档没有特定范围，但全空间查询仍可见。
- 修改 Key 只改关联关系，不重新解析、切片或向量化。

普通 PAT 模式下，Scope Key 不承担安全隔离，`document_selection` 只是每次调用选择
哪些文档集合。第三方系统需要把自己选定的文档范围交给外部 AI 自主探索时，应创建
短期探索凭证，由服务端把该选择固化为不可扩大的 MCP 边界。

## 2. 创建令牌

在“设置 → 外部接入”创建令牌，选择工作空间并按需要授予：

| Scope | 用途 |
|---|---|
| `knowledge:read` | 正文、片段、证据、原文、预览、数据集结构 |
| `knowledge:search` | 检索和数据集精确查询 |
| `knowledge:ask` | 快速/深度问答 |
| `documents:write` | 上传文档、修改 Scope Key |

绑定空间的令牌不接受切换空间。旧的未绑定令牌仍可用 `X-Cangzhi-Workspace` 选择空间。
现有 PAT 可以继续直接连接 `/api/mcp`，行为不变，并可探索其整个工作空间。

## 3. 上传

```bash
curl -X POST 'http://localhost:8000/api/v1/documents' \
  -H 'Authorization: Bearer cz_pat_xxx' \
  -F 'file=@manual.pdf' \
  -F 'title=操作手册' \
  -F 'external_id=manual-2026' \
  -F 'scope_keys=["id-a","id-b"]'
```

响应为 `202 Accepted`，包含 `document_id`、`version_id`、`status=queued` 和状态地址。
同一工作空间内 `external_id` 唯一：相同内容返回 `unchanged`；内容变化沿用文档并新增版本，
Scope Key 默认保持，只有本次明确传入时才整体替换。

上传令牌可直接轮询响应中的 `status_url`（`GET
/api/v1/documents/{document_id}/processing-status`），查看解析、AI 整理、切片和向量化各
阶段进度；不需要管理后台的登录 Cookie。

`scope_keys` 可使用 JSON 字符串数组，也可使用重复的 multipart 字段。每个 Key 最长
128 字符，每篇文档最多 100 个。

## 4. 管理 Scope Key

```http
GET /api/v1/documents/{document_id}/scope-keys
PUT /api/v1/documents/{document_id}/scope-keys
```

```json
{"scope_keys":["id-a","id-c"]}
```

`PUT` 是整体替换；传空数组清除全部关联。批量同步：

```http
PUT /api/v1/document-scope-keys/batch
```

```json
{
  "items": [
    {"external_id":"manual-2026","scope_keys":["id-a"]},
    {"document_id":42,"scope_keys":[]}
  ]
}
```

批量接口在同一事务内处理，任一文档不存在则整体失败。

## 5. 查询

Search、Ask、Deep 和对应 MCP 工具使用同一结构：

```json
{
  "query": "报销流程",
  "document_selection": {
    "scope_keys": ["id-a", "id-b"],
    "document_ids": [42, 73]
  }
}
```

候选集合固定为：

```text
documents(scope_key in [id-a, id-b]) UNION documents(id in [42, 73])
```

分类、标签、来源、连接器和保存的 KnowledgeScope 再与候选集合取交集。省略
`document_selection` 表示整个工作空间；显式传入空对象返回
`400 invalid_document_selection`，绝不回退全空间。Scope Key 和 ID 不存在时只产生空
候选，不扩大范围。

## 6. MCP 与 CLI

MCP 的 `knowledge_search`、`knowledge_ask`、`knowledge_list_facets` 和
`knowledge_list_datasets` 均接受 `document_selection`。MCP 只提供读取、检索和问答；
上传与 Scope Key 管理走 REST。

MCP 有两种凭证模式：

1. **普通 PAT**：保持原有方式，工作空间是硬边界；工具参数中的
   `document_selection` 是可选查询条件。
2. **短期探索凭证**：第三方后端使用绑定空间的 PAT 调用下列接口创建。凭证只能访问
   创建时选中的文档，MCP 工具参数只能继续收窄，不能扩大；按 ID 读取文档、片段、
   数据集和证据同样受此边界约束。

```bash
curl -X POST 'http://localhost:8000/api/v1/exploration-grants' \
  -H 'Authorization: Bearer cz_pat_xxx' \
  -H 'Content-Type: application/json' \
  -d '{
    "document_selection": {
      "scope_keys": ["id-a", "id-b"],
      "document_ids": [42, 73]
    },
    "ttl_seconds": 3600
  }'
```

响应中的 `cz_eg_...` 明文只返回一次。第三方后端保管 PAT，仅把短期凭证作为
`Authorization: Bearer cz_eg_...` 交给 MCP 客户端，连接地址仍为 `/api/mcp`，不需要
在工具参数里重复传边界。探索凭证不能调用普通 REST v1 端点，最长有效 24 小时；撤销：

```http
POST /api/v1/exploration-grants/{grant_id}/revoke
Authorization: Bearer cz_pat_xxx
```

藏知数据库只保存凭证哈希，不保存可再次展示的明文；响应和日志只呈现安全前缀及范围
数量，不呈现 Scope Key 值。创建它的 PAT 被撤销或删除后，派生凭证也立即失效。

CLI 可使用：

```bash
cangzhi search '报销流程' --scope-keys id-a,id-b --document-ids 42,73
```

## 7. 生命周期与审计

- 文档更新：保留 Key。
- 文档进入回收站：检索自动排除，Key 关系保留以便恢复。
- 文档永久删除：Key 关联级联删除。
- 修改 Key：不触发 AI 整理、切片或向量重建。
- Key 不包含密钥语义，不应使用手机号、身份证号等直接个人信息。
- 探索凭证到期或被撤销后立即失效；Scope Key 关联变化在下一次 MCP 调用即时生效。
