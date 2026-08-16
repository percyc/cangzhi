# 外部系统接入：上传与文档检索范围

本文描述外部业务系统如何把藏知作为知识中枢。设计决策见
[ADR-021](ADR-021-external-client-access.md)，完整字段以 [API 参考](API_REFERENCE.md) 为准。

## 1. 模型与语义

```text
PAT ──绑定──> Workspace
Document <──多对多──> opaque access_key（id-a / id-b / ...）
```

- Access Key 由外部系统生成和维护，藏知不登记用户、角色或 Key 名录。
- 一篇文档可以属于多个 Key。
- 不传 Key：查询工作空间全部文档。
- 传 `id-a`：只查询关联 `id-a` 的文档。
- 空 `access_keys`：文档没有特定范围，但全空间查询仍可见。
- 修改 Key 只改关联关系，不重新解析、切片或向量化。

这是一种检索范围，不是完整权限系统。外部系统必须在自己的服务端完成用户鉴权，并把
经过判断的 Key 注入藏知请求。不要让浏览器用户或大模型任意填写其他 Key。

## 2. 创建令牌

在“设置 → 外部接入”创建令牌，选择工作空间并按需要授予：

| Scope | 用途 |
|---|---|
| `knowledge:read` | 正文、片段、证据、原文、预览、数据集结构 |
| `knowledge:search` | 检索和数据集精确查询 |
| `knowledge:ask` | 快速/深度问答 |
| `documents:write` | 上传文档、修改 Access Key |

绑定空间的令牌不接受切换空间。旧的未绑定令牌仍可用 `X-Cangzhi-Workspace` 选择空间。

## 3. 上传

```bash
curl -X POST 'http://localhost:8000/api/v1/documents' \
  -H 'Authorization: Bearer cz_pat_xxx' \
  -F 'file=@manual.pdf' \
  -F 'title=操作手册' \
  -F 'external_id=manual-2026' \
  -F 'access_keys=["id-a","id-b"]'
```

响应为 `202 Accepted`，包含 `document_id`、`version_id`、`status=queued` 和状态地址。
同一工作空间内 `external_id` 唯一：相同内容返回 `unchanged`；内容变化沿用文档并新增版本，
Access Key 默认保持，只有本次明确传入时才整体替换。

上传令牌可直接轮询响应中的 `status_url`（`GET
/api/v1/documents/{document_id}/processing-status`），查看解析、AI 整理、切片和向量化各
阶段进度；不需要管理后台的登录 Cookie。

`access_keys` 可使用 JSON 字符串数组，也可使用重复的 multipart 字段。每个 Key 最长
128 字符，每篇文档最多 100 个。

## 4. 管理 Access Key

```http
GET /api/v1/documents/{document_id}/access-keys
PUT /api/v1/documents/{document_id}/access-keys
```

```json
{"access_keys":["id-a","id-c"]}
```

`PUT` 是整体替换；传空数组清除全部关联。批量同步：

```http
PUT /api/v1/document-access-keys/batch
```

```json
{
  "items": [
    {"external_id":"manual-2026","access_keys":["id-a"]},
    {"document_id":42,"access_keys":[]}
  ]
}
```

批量接口在同一事务内处理，任一文档不存在则整体失败。

## 5. 查询

请求体方式：

```json
{"query":"报销流程","access_key":"id-a"}
```

固定连接范围更适合用请求头：

```http
X-Cangzhi-Access-Key: id-a
```

若请求头和请求体同时提供且不一致，返回 `400 access_key_conflict`。该范围覆盖 REST 的
search、quick/deep/stream ask、数据集查询、正文与证据读取，也覆盖 MCP 对应工具。

## 6. MCP 与 CLI

MCP 连接可以固定请求头；工具参数也支持可选 `access_key`。共享给最终用户时推荐由外部
系统固定请求头，因为工具参数可能由模型生成。MCP 只提供读取、检索和问答；上传与范围
管理走 REST。

CLI 可使用：

```bash
export CANGZHI_ACCESS_KEY='id-a'
cangzhi search '报销流程'
```

## 7. 生命周期与审计

- 文档更新：保留 Key。
- 文档进入回收站：检索自动排除，Key 关系保留以便恢复。
- 文档永久删除：Key 关联级联删除。
- 修改 Key：不触发 AI 整理、切片或向量重建。
- Key 不包含密钥语义，不应使用手机号、身份证号等直接个人信息。
