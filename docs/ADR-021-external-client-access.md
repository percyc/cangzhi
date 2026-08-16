# ADR-021：工作空间令牌与文档 Access Key

**状态**：已接受并实现 · **关联说明**：[外部系统接入](EXTERNAL_CLIENT_ACCESS.md)

## 背景

外部系统需要上传文档，并用自身已有的业务标识（例如 `id-a`、`id-b`）决定一次调用
可以检索哪些文档。一篇文档可以关联多个标识。同时，藏知仍是单管理员的个人知识中枢，
不应为此引入外部用户、角色、组织和复杂权限生命周期。

## 决策

1. PAT 可选绑定一个工作空间。绑定后忽略 Cookie，且请求头或查询参数不能切换到其他
   空间；未绑定旧令牌保持兼容。
2. 文档与不透明字符串 `access_key` 通过 `document_access_keys` 多对多关联。不存在
   Access Key 注册表，也不复用内容标签。
3. 不传 `access_key` 表示检索令牌工作空间全部文档；传入时只返回关联该 Key 的文档。
   没有关联 Key 的文档只会出现在全空间查询中。
4. `access_key` 是受信任外部系统提供的检索范围，不是藏知自己的用户身份认证。外部
   系统负责确认当前用户能够使用哪个 Key。
5. Access Key 作为 KnowledgeScope、分类、标签、来源、连接器和文档 ID 过滤之上的
   额外交集，用 SQL `EXISTS` 下推到全文、向量和数据集发现查询。
6. 搜索、快速/深度/流式问答、MCP、数据集、证据、正文、原文与预览共享相同范围语义。
7. `documents:write` 仅允许上传文档和管理 Access Key；MCP 保持只读取证，不开放写操作。

## 数据模型

```text
personal_access_tokens.workspace_id? -> workspaces.id

document_access_keys
  workspace_id -> workspaces.id
  document_id  -> documents.id
  access_key   varchar(128)
  unique(document_id, access_key)
  index(workspace_id, access_key, document_id)
```

关联绑定文档实体而不是版本，因此文档更新不会丢失范围；永久删除文档时数据库级联删除。

## 安全边界

- 如果最终用户或模型能任意改变 `access_key`，它就不能承担安全隔离。共享 MCP 场景应由
  外部系统固定 `X-Cangzhi-Access-Key`，而不是让模型自行决定。
- 不传 Key 的调用拥有空间全局视图，因此只应向可信后台签发或保管相应令牌。
- 真正出现多用户、成员共享和角色权限时，另行建设 workspace membership；不把本机制
  演化成隐式 ACL。

## 重新评估条件

出现真实的多管理员、用户自行直连藏知、一个令牌只能访问若干 Key、或需要组织继承与
拒绝规则时，升级为显式授权模型；在此之前保持当前轻量设计。
