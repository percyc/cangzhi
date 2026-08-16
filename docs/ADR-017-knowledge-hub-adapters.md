# ADR-017：藏知作为 API-first 知识中枢

状态：已接受（2026-07-31）

> 演进说明：本 ADR 的只读底座仍有效；文件上传与文档 Access Key 已由
> [ADR-021](ADR-021-external-client-access.md) 以独立 `documents:write` 能力补充。

## 背景

藏知目前由 FastAPI、Next.js、Worker、PostgreSQL 与对象存储组成。网页已能采集、
解析、分类、混合检索和问答，但接口仍主要服务网页：业务路由没有版本号，只支持
浏览器 Cookie，会话式问答、CLI、MCP 和外部智能体接入尚未形成统一契约。

目标是让网页、CLI、Hermes、OpenClaw 及其他智能体共享同一套知识能力，同时保持
当前单用户部署简单、安全、可维护。

## 决策

### 1. 核心能力与适配器分离

业务能力只在 `apps/api/services/` 实现：

- 知识范围解析；
- 混合检索、读取文档与证据；
- 问答与引用校验；
- 采集、分类和解析；
- 后续的对话线程。

REST v1、现有网页 API、CLI 与 MCP 都是适配器，只负责鉴权、DTO、错误映射和
传输，不复制检索或问答逻辑。适配器不得自行拼接 SQL 或直接调用模型。

首批稳定只读能力：

- `GET /api/v1/capabilities`
- `GET /api/v1/knowledge/scopes`
- `GET /api/v1/knowledge/facets`
- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/ask`
- `GET /api/v1/knowledge/documents/{document_id}`
- `GET /api/v1/knowledge/chunks/{chunk_id}`

既有 `/api/*` 暂时保留并继续服务网页，不宣告固定下线日期。网页会逐步切换到
公共 service 或 v1 契约；在完成迁移前不做重定向。

### 2. KnowledgeScope 是一等对象

`KnowledgeScope` 表达一次检索或问答允许使用的知识集合。它既可临时随请求提交，
也可保存并用稳定 ID/slug 引用。

范围可包含：

- `category_ids`
- `tag_ids`
- `source_types`
- `connector_ids`
- `document_ids`

系统范围 `all`、`notes`、`web`、`files` 由代码定义，不依赖初始化脚本和数据库
播种；用户保存的范围存入 `knowledge_scopes`。回收站不作为可问答范围。

请求同时携带 `scope_id` 和临时过滤条件时，临时条件只能进一步收窄范围：

- 同一维度取交集；
- 只在一侧存在的维度直接生效；
- 交集为空时返回空结果，而不是悄悄扩大范围。

服务层统一完成 slug/ID 校验、连接器到文档集合的解析和活跃文档约束。所有检索
入口必须复用它，避免网页、CLI 与 MCP 得到不同结果。

首个版本继续使用 JSON 数组保存范围成员。这是单用户、个人规模下的合理折中；
服务层负责引用校验。出现团队共享或百万级范围成员时，再迁移关联表。

### 3. 无状态取证与持久问答记录并存

`search` 和 `ask` 的推理语义保持无状态，适合网页、CLI、自动化和外部智能体。
网页可显式提交 `conversation_id`，把结果快照保存到独立的 `ask_conversations`、
`ask_turns`；不提交时仍是纯无状态调用。

每轮都根据当前问题和当轮 `KnowledgeScope` 重新检索。历史消息仅用于回看，不参与
代词消解或模型提示词，也不复用旧证据代替新检索；记录保存实际引用的
document/version/chunk 快照，保证之后仍能解释答案来源。

对话页默认是聊天界面：

- 顶部选择“回答范围”，默认全库；
- 可选择保存的范围、分类、指定文档或连接器；
- 每轮明确显示使用的范围，后续提问可重新选择；
- 每条回答就近显示引用，搜索页仍负责探索和精确筛选。

### 4. 外部智能体默认取证，藏知问答为可选能力

Hermes、OpenClaw 等本身已有推理模型，因此默认提供：

- 检索证据；
- 读取指定文档；
- 读取指定 chunk；
- 列出知识范围和能力。

这些接口不调用对话模型，可避免“双模型回答”、额外费用和风格冲突。
`knowledge.ask` 仍保留，供调用方明确希望由藏知完成 RAG 回答时使用；其响应必须
清楚标注使用了模型，并返回可核验引用。

知识正文与网页内容始终按“不可信数据”处理。模型系统提示明确规定证据中的指令
不得执行。服务端保留原文用于引用，不通过修改原文字节、删除 Markdown 或插入
零宽字符来“清洗”证据，以免破坏事实源。

### 5. 浏览器 Cookie 与个人访问令牌并存

当前产品仍是单管理员，不提前引入 `principal`、组织或租户表。

新增 `personal_access_tokens`，直接归属 `admin_id`：

- 明文格式 `cz_pat_<random>`，只在创建时展示一次；
- 数据库只保存 SHA-256、可识别前缀、名称、权限、到期/撤销/最近使用时间；
- 支持轮换和撤销，不支持读取旧明文；
- 日志、错误和审计不得输出令牌。

首批权限：

- `knowledge:read`：读取文档/chunk/范围；
- `knowledge:search`：执行检索；
- `knowledge:ask`：调用藏知模型问答。

Cookie 登录的管理员拥有全部能力。`/api/v1` 使用统一的
`require_api_identity(required_scopes)`，可接受有效 Cookie 或 Bearer PAT；
既有 `/api` 继续使用 `require_admin`，不破坏现有前端和测试依赖覆盖。

令牌管理属于“设置 → 接入与安全”，只允许 Cookie 管理员创建、撤销。PAT 不允许
创建或管理其他 PAT，防止权限自扩张。

### 6. v1 契约

成功响应使用业务对象本身，不额外套多层 envelope；目录列表统一为：

```json
{"items": []}
```

错误统一为：

```json
{
  "detail": {
    "code": "stable_machine_code",
    "message": "可读说明"
  }
}
```

首批引用至少包含：

- `document_id`
- `document_version_id`
- `chunk_id`
- `title`
- 章节路径、页码、段落和 source span
- `snippet`

问答引用在界面内通过证据抽屉核验，不要求离开当前对话。抽屉读取必须同时提交
`chunk_id` 与回答携带的 `document_version_id`；服务端只返回该版本的正文、版式预览
或原文件，禁止用当前最新版替代历史证据。文档型证据返回格式化章节与明确的引用
段落；PDF/Word 第一阶段按页定位版式文件并并排显示解析段落，精确坐标框属于后续
增强。

数据型引用额外携带 `evidence_type`、`dataset_id`、`artifact_version`、受控
`query_plan`、`columns` 和精确 `source_rows`。证据明细接口只读取这些明确行号，
单次不超过 200 行，不把 `row_start..row_end` 的连续范围冒充实际贡献行，也不返回
内部 SQL。聚合引用同时保留筛选、分组、指标和计算结果；贡献行超过上限时必须标注
为受控样本。

同一 v1 主版本内只允许增加可选字段，不删除/改名字段，不改变字段含义。破坏性
变更进入 v2。首批搜索采用 `limit/offset`；需要大规模遍历时再增加稳定 cursor，
且不破坏现有字段。`content_hash`、统一 locator 和 request ID 可作为向后兼容的
可选字段继续补充。

### 7. CLI

`cangzhi` CLI 是 REST v1 的薄客户端，不直连数据库。首批从项目目录运行
`python -m apps.cli`：

- `python -m apps.cli capabilities`
- `python -m apps.cli scopes`
- `python -m apps.cli search QUERY [--scope SLUG]`
- `python -m apps.cli ask QUESTION [--scope SLUG]`
- `python -m apps.cli document ID`
- `python -m apps.cli chunk ID`

标准输出默认就是稳定 JSON；`--compact` 可改为单行。地址和令牌优先从
`CANGZHI_URL`、`CANGZHI_TOKEN` 读取；不得在日志或错误中打印完整令牌。
写入/采集命令在只读接口稳定后另行增加。

### 8. MCP

首版采用无状态 MCP Streamable HTTP，并复用 REST v1 service 与 PAT 鉴权。
部署层继续限制请求大小与超时；跨域暴露、Origin 白名单和独立速率限制在需要将
端点发布到公网时启用。

只读 tools：

- `knowledge_search`
- `knowledge_ask`
- `knowledge_list_facets`
- `knowledge_get_document`
- `knowledge_get_chunk`
- `knowledge_list_scopes`

`knowledge_search` 默认只取证，不调用藏知对话模型；`knowledge_ask` 与 REST 的
`knowledge/ask` 复用同一问答服务、结构化表格计算和引用校验，并要求令牌显式包含
`knowledge:ask`。`knowledge_get_document` 默认只返回 12000 字符窗口，并通过
`content_window.next_offset` 分页；不会把完整 `structured_content` 注入外部模型。
MCP resources 在客户端兼容性验证后按需增加。

Skill 只封装“何时调用、如何选择范围、如何呈现引用”，不包含检索逻辑。提供一份
Hermes、OpenClaw 及兼容 Skills 平台均可使用的最小模板。

### 9. 幂等、审计与安全

首批接口只读，不引入通用幂等表。未来采集/写入接口必须支持
`Idempotency-Key`，并与现有 `processing_jobs.idempotency_key` 协同设计。

首批审计记录 PAT 最近使用时间，且永不记录明文。以下调用级审计在接口对公网或
多用户开放前增加：

- PAT ID（不记录明文）；
- 路由、状态码、耗时、request ID；
- KnowledgeScope ID/摘要；
- 查询文本的不可逆哈希；
- 错误码。

不记录问题全文、证据正文或模型密钥。审计写入失败不影响业务响应，但必须告警。

外部输入限制长度并按纯数据传递；证据与用户文本不能进入 system role。服务端
校验 PAT scope、文档存活状态和 chunk 归属，防止通过已知 ID 越权读取回收站或
其他范围之外的内容。

## 实施阶段与验收

### 阶段 A：公共只读底座

交付 `KnowledgeScope`、PAT、`/api/v1` 只读接口和设置中的令牌管理。

验收：

- 旧 `/api` 和网页登录流程保持全绿；
- Cookie 与正确 scope 的 PAT 均能调用 v1；
- 无凭据为 401，权限不足为 403，撤销/过期令牌为 401；
- scope 叠加过滤只会收窄结果；
- 文档/chunk 读取不返回已删除内容；
- OpenAPI 与端到端测试覆盖稳定错误和引用字段。

### 阶段 B：CLI

交付薄客户端、`--json` 契约和安装文档。用真实部署完成 status、范围列表、检索、
读取文档、问答的冒烟测试。

### 阶段 C：只读 MCP 与接入模板

交付 Streamable HTTP、五个只读取证 tools、通用 Skill 模板与安全测试。验证取证
工具不调用对话模型。

### 阶段 D：持久问答记录

交付记录/轮次数据模型和聊天式 `/ask`。验证每轮独立检索、范围快照、引用可回溯，
并确保删除记录不会影响知识原文。

### 阶段 E：受控写入

完成只读安全审计后，再开放 URL、随手记、文件采集；增加幂等、任务状态与
`knowledge:write`，默认不授予外部智能体。

## 后果

优点：

- 网页、CLI 和 Agent 使用同一能力和引用语义；
- 外部智能体可以只取证据，不被迫再经过一次藏知模型；
- 当前单用户模型保持简单，同时 PAT/admin 外键可在未来迁移到用户/工作区；
- v1 提供可验证的兼容边界。

代价：

- 需要维护公共 DTO、令牌生命周期和审计；
- 保存范围的 JSON 成员由应用层维护引用完整性；
- 在网页迁移完成前，旧 API 与 v1 会短期并存。

重新评估条件：引入多用户/共享空间、单范围达到十万成员、需要跨租户授权，或
外部调用量要求分布式限流时，重新设计工作区、关联表和统一身份层。
