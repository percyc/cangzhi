# ADR-021：工作空间令牌与文档 Scope Key

**状态**：已接受并实现 · **关联说明**：[外部系统接入](EXTERNAL_CLIENT_ACCESS.md)

## 背景

外部系统需要上传文档，并用自身已有的业务标识（例如 `id-a`、`id-b`）决定一次调用
可以检索哪些文档。一篇文档可以关联多个标识。同时，藏知仍是单管理员的个人知识中枢，
不应为此引入外部用户、角色、组织和复杂权限生命周期。

## 决策

1. PAT 可选绑定一个工作空间。绑定后忽略 Cookie，且请求头或查询参数不能切换到其他
   空间；未绑定旧令牌保持兼容。
2. 文档与不透明字符串 `scope_key` 通过 `document_scope_keys` 多对多关联。不存在
   Scope Key 注册表，也不复用内容标签。
3. PAT 是唯一工作空间授权；Scope Key 只是文档分组元数据，不是凭证或 ACL。
4. Search/Ask/MCP 使用可选 `document_selection`。其中任意 Scope Key 命中的文档与
   明确 Document ID 取并集并去重；不传表示全工作空间，显式空选择是错误。
5. `document_selection` 通过 SQL `EXISTS(scope_key IN ...) OR Document.id IN (...)`
   下推；KnowledgeScope、分类、标签、来源和连接器继续与候选集合取交集。
6. 搜索、快速/深度/流式问答、MCP 和数据集发现共享该选择语义。普通 PAT 下，正文、
   片段和证据的明确 ID 读取只受工作空间隔离；需要把一次选择固化为外部 AI 不可越过的
   MCP 边界时，使用 [ADR-022](ADR-022-mcp-exploration-grants.md) 的短期探索凭证。
7. `documents:write` 仅允许上传文档和管理 Scope Key；MCP 保持只读取证，不开放写操作。

## 数据模型

```text
personal_access_tokens.workspace_id? -> workspaces.id

document_scope_keys
  workspace_id -> workspaces.id
  document_id  -> documents.id
  scope_key   varchar(128)
  unique(document_id, scope_key)
  index(workspace_id, scope_key, document_id)
```

关联绑定文档实体而不是版本，因此文档更新不会丢失范围；永久删除文档时数据库级联删除。

## 安全边界

- Scope Key 可以由外部系统或 Agent 用于探索，但不能承担用户授权。
- PAT 持有者可以读取其绑定工作空间；外部系统必须自行保管 PAT 并完成用户鉴权。
- 真正出现多用户、成员共享和角色权限时，另行建设 workspace membership；不把 Scope
  Key 演化成隐式 ACL。

## 重新评估条件

出现真实的多管理员、用户自行直连藏知、一个令牌只能访问若干 Key、或需要组织继承与
拒绝规则时，升级为显式授权模型；在此之前保持当前轻量设计。
