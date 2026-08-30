# 藏知技术架构

本文描述当前代码已经实现的系统结构、数据流和边界。产品为什么存在见
[产品定位](PRODUCT.md)，部署和运维见[部署指南](DEPLOYMENT.md)，历史技术取舍见
[关键技术决策](DECISIONS.md)。

## 1. 架构定位

藏知是一个面向个人部署的模块化单体，由 Web、API、Worker、PostgreSQL 和本地文件
存储组成。它的核心不是聊天页面，而是：

> 可追溯的知识事实源 + 可重建的理解与索引 + 面向人和 Agent 的统一取证服务。

系统遵守五条不变量：

1. 原文件、网页快照、随手记原文和文档版本是事实源。
2. 摘要、分类、切片、向量、Parquet 和 PDF 预览都是可重建派生物。
3. 解析或模型失败不能导致已经接收的原文丢失。
4. 网页、REST、CLI、MCP 和 Skill 复用同一知识范围、检索和证据语义。
5. 外部来源、对话模型和向量模型可以替换，不能锁定知识资产。

当前采用“单管理员、多工作空间”。工作空间是知识隔离边界，不是租户或权限角色。
未明确选择时统一落入 `default`，从而兼容升级前的全部数据和调用方。

## 2. 运行拓扑

```text
浏览器 ───────────────┐
                      v
外部 Agent / CLI ─> Web(Next.js) ──同源 /api──> API(FastAPI)
                     :3000                       :8000
                                                    │
                    ┌───────────────────────────────┼──────────────┐
                    v                               v              v
            PostgreSQL + pgvector             storage/       模型服务
              元数据/任务/全文/向量            原文/预览/       OpenAI-compatible
                    ^                         Parquet         或 Ollama
                    │
              Worker(Python)
          解析/理解/切片/向量/预览
```

- `web`：界面、登录引导和同源 API 转发，不保存业务事实。
- `api`：鉴权、知识管理、检索、问答、数据集执行和外部接口。
- `worker`：从数据库领取幂等后台任务，处理耗时工作。
- `postgres`：PostgreSQL 16 + pgvector，是事务、任务和检索状态中心。
- `storage/`：内容寻址的 Blob、网页快照、PDF 预览、Parquet 以及 `.secret_key`。
- `migrate`：每次 Compose 启动前执行 Alembic，成功后 API 才启动。

API 和 Worker 共享同一 `storage/`，数据库使用 Docker named volume。完整恢复必须同时
恢复两者，详见[备份与恢复](BACKUP_AND_RESTORE.md)。

## 3. 代码边界

```text
apps/
├── web/          Next.js 页面、组件、同源代理
├── api/
│   ├── api/      HTTP / REST v1 / MCP 适配器
│   ├── models/   SQLAlchemy 持久模型
│   ├── services/ 检索、问答、证据、数据集等领域服务
│   ├── parsers/  格式专用解析器
│   ├── security/ 会话、PAT、加密、URL 安全
│   └── storage/  Blob 存储抽象
├── worker/       数据库任务领取与后台处理
└── cli/          REST v1 薄客户端

migrations/       Alembic 迁移
integrations/     Skill 模板和接入说明
packages/         提示词与跨模块资源
scripts/          备份、校验和恢复演练
storage/          运行数据，不进入 Git
```

HTTP 层只做输入校验、鉴权和 DTO 转换；检索、范围解析、问答、引用和数据集执行必须
放在服务层，避免网页、MCP 和 CLI 出现不同答案。

## 4. 知识生命周期

### 4.1 采集与版本

入口包括链接、上传文件、随手记和 WebDAV。一次采集会创建：

- `documents`：用户看到的逻辑知识对象；
- `document_versions`：不可变内容版本和当前处理状态；
- `blobs`：按 SHA-256 去重的原文件或网页快照；
- `processing_jobs`：后续幂等任务。

`documents.current_version_id` 指向当前生效版本。更新内容创建新版本，不覆盖旧版本。
回收站只改变文档可见性；永久删除才级联清理版本和派生数据，并且仅在 Blob 无其他
引用时删除物理文件。

### 4.2 后台处理管线

```text
stored/parsing
      │
      ├─> understanding ─> 摘要、分类、标签
      ├─> chunking ──────> parent/child 切片与全文索引
      ├─> dataset_catalog -> 表格区域、字段画像、结构化行
      ├─> dataset_artifact -> Parquet 活跃产物
      ├─> preview ───────> Word 等格式的 PDF 预览
      └─> embedding ─────> 当前或构建中向量档案
```

任务以 `document_version + stage + config_version` 为幂等边界，状态包含 `created`、
`processing`、`retry`、`completed` 和 `failed`。失败记录阶段、重试次数、下次执行时间
和脱敏错误；确定性格式上限错误不会无意义重试。

AI 理解不是解析的前置条件。模型未配置或暂时不可用时，原文、基础解析、全文切片和
人工管理仍可用，理解任务进入可观察的降级或待处理状态。

## 5. 双引擎知识模型

### 5.1 文档引擎

PDF、Word、Markdown、网页和随手记按章节、条款、段落、列表与表格边界解析。
`document_chunks` 保存两种当前切片：

- `child`：用于关键词和向量召回；
- `parent`：用于补充完整上下文。

切片保留 `heading_path`、页码、段落号和 `source_start/source_end`。过长自然段先按
句子边界切分，必要时才硬切，并携带有限重叠，降低信息在边界处被截断的风险。

原文件始终可下载。浏览时优先使用原生 PDF；Word 等格式可生成一次性 PDF 预览，
预览不是事实源。Markdown 使用安全渲染；引用通过文档版本和切片位置回到原文。

### 5.1.1 扫描 PDF 的文字兜底

PDF 解析默认走 `pypdf` 的可复制文本通道；只有当一页去除空白后非空字符
数低于 `PDF_OCR_MIN_NATIVE_CHARS`、且该页资源里至少有一个 `/XObject` 图像
时，才被视作扫描候选页。Worker 容器安装 `pypdfium2` 与 `tesseract-ocr`
（含 `chi_sim`、`eng` 训练数据），按 `PDF_OCR_DPI` 渲染候选页为 PNG，
通过 `tesseract stdin stdout -l <lang> --psm 6 tsv` 拿到词级结果，再按
`block_num/par_num/line_num` 聚合成行级段落。词级坐标转换为 PDF 点坐标
（Y 轴向上），作为 `extra.bbox` 保留在切片元数据里。

OCR 关闭、依赖缺失或超过 `PDF_OCR_MAX_PAGES` 候选页时，按页记录到
`metadata.pdf_extraction.ocr_skipped_pages` 与 `ocr_skipped_reasons`，
整体解析仍标记为成功，原始 PDF 不受影响。结构化 `pdf_extraction` 摘要
会附加在 `processing_status` 的 `parsing.extraction` 上，文档详情页
展示原生文字页、OCR 完成、失败、跳过数量，失败不阻塞其余阶段。图片
语义理解、图表识别与版面重建仍属后续阶段。

### 5.2 数据集引擎

XLS/XLSX 二维数据仍属于文档生命周期，但精确查询不依赖普通文本切片：

```text
工作簿版本
  ├─ knowledge_datasets    工作表中的二维区域
  ├─ dataset_fields        字段类型、样例与质量画像
  ├─ structured_table_rows 兼容行索引
  └─ dataset_artifacts     可重建 Parquet 版本
                              │
                              └─ DuckDB 受控查询
```

向量只负责发现相关数据集、字段和少量代表行。筛选、投影、排序、分组、计数、去重、
求和、平均和最值由 DuckDB 下推到当前 Parquet 产物。API 只接受白名单查询计划，不
接受模型生成的任意 SQL 或代码，并限制内存、线程、超时、分页和最大返回行数。

大型表格不会为每一行制造一个文档切片或向量；一个数据区域只建立紧凑目录切片。
回答引用工作表、查询条件和证据原始行，避免用代表行冒充全量计算结果。

外部 PostgreSQL/MySQL 也通过同一数据集引擎进入藏知。连接器只接受分离的主机、端口、
库名和凭据，不接受 DSN 或任意 SQL；运行时先从 `information_schema` 取得 Schema、表和
字段，再由用户选择整表/视图创建本地快照。远端会话强制只读并设置连接与语句超时，
密码加密保存，来源与快照按工作空间隔离。当前单表上限为 10 万行；更大的数据应先在
来源库建立只读视图，后续版本再增加增量同步与可视化筛选导入。

导入后，行数据进入 `structured_table_rows`，同时生成 Parquet 供 DuckDB 执行；
`DocumentVersion` 只保留字段和快照摘要，避免为大表再复制一份完整 JSON/文本。
重导入沿用同一文档并产生新版本，新 Parquet 提交成功后才清理旧产物。

## 6. 检索、问答与证据

### 6.1 混合检索

```text
查询 + KnowledgeScope/临时筛选
      ├─ PostgreSQL 全文召回
      └─ 当前 Embedding Profile 向量召回
                    ↓
                 RRF 融合
                    ↓
          文档去重、父片段扩展、预算裁剪
                    ↓
                 引用证据
```

分类、标签、来源、连接器和指定文档在两路召回中使用相同约束。向量调用或查询失败
时自动降级到全文搜索，并在响应中返回实际检索模式和降级原因。

Embedding 配置与服务索引分离。候选配置先测试固定 canary；需要重建时在后台构建
新 Profile，旧 Profile 继续服务。覆盖完成后原子启用，并保留可回滚版本。

### 6.2 快速问答与深度分析

- `quick`：一次混合检索；表格问题可进入一次受控数据集计划；随后生成带引用回答。
- `deep`：Plan–Act–Observe 循环选择搜索、读片段、发现数据集、读 Schema 或执行
  受控查询。默认最多 12 次有效工具调用，持续获得新证据时最多扩展至 16 次，并受
  总时限和停滞检测约束。模型准备结束探索时，独立的 AI 证据审计会检查关键事实、
  来源冲突、版本与时间适用关系、范围口径和精确计算是否闭环；如仍有可补足的缺口，
  审计生成一条针对缺失连接事实的新检索并继续循环，而不是重复已有结论。

深度分析通过 NDJSON 流式发送工具动作和证据审计摘要，不展示模型私有思维链。
这里的“AI 思维链”指模型实际参与规划、观察、纠偏和证据闭环，而不是向用户输出
不可核验的内部推理文本。
最终答案只能引用本轮实际读到的文档版本、切片或数据行；证据不足时必须明确说明。

### 6.3 问答记录不是模型记忆

`ask_conversations` 和 `ask_turns` 保存问题、回答、知识范围、引用和执行摘要，供跨
设备回看、删除和审计。每次请求仍只把当前问题发送给问答服务，不自动读取历史轮次。

连续推理和长期个性化记忆由 Hermes、OpenClaw 等外部 Agent 负责；藏知通过 MCP、
Skill 或 REST 提供可追溯证据。这能避免界面像聊天就让用户误以为模型记住了上文，
也避免旧答案静默污染新检索。

## 7. 统一接入层

```text
浏览器业务 API ─┐
REST v1 ─────────┼─> KnowledgeScope -> search/read/dataset/ask -> evidence
CLI ─────────────┤
MCP / Skill ─────┘
```

- 浏览器 API：Cookie 鉴权，服务产品管理页面，可随 UI 演进。
- REST v1：Cookie 或 Bearer PAT，提供稳定知识读取、受控文件上传与 Scope Key 管理契约。
- CLI：REST v1 的 JSON 客户端，不直连数据库。
- MCP：Streamable HTTP，只读取证工具优先；`knowledge_ask` 是可选快速回答。
- Skill：指导外部 Agent 自主组合搜索、读取和数据集查询，不复制业务逻辑。

外部 Agent 默认只申请 `knowledge:read` 和 `knowledge:search`。只有明确需要藏知内部
模型回答时才授予 `knowledge:ask`，避免外部模型与藏知模型重复推理和计费。详细契约
见[外部接入](INTEGRATIONS.md)。

浏览器以 `cangzhi_workspace` Cookie 选择当前空间；REST、CLI、MCP 和 Skill 使用
`X-Cangzhi-Workspace` 请求头。解析顺序是请求头、查询参数、Cookie、`default`。
不存在的空间返回 404，已归档空间返回 409。后台任务根据所属文档重新绑定空间，
避免复用 Worker 会话时发生分类或标签串库。

## 8. 当前核心数据表

| 领域 | 当前表 |
|---|---|
| 身份、空间与模型 | `admins`、`auth_sessions`、`personal_access_tokens`、`workspaces`、`ai_runtime_configs` |
| 文档事实源 | `documents`、`document_versions`、`blobs` |
| 处理与检索 | `processing_jobs`、`document_chunks`、`embedding_profiles`、`chunk_embeddings` |
| 组织 | `categories`、`document_categories`、`tags`、`document_tags`、`document_summaries`、`tag_merge_records` |
| 数据集 | `knowledge_datasets`、`dataset_fields`、`structured_table_rows`、`dataset_artifacts` |
| 外部来源 | `webdav_sources`、`webdav_entries`、`external_item_exclusions` |
| 范围与问答 | `knowledge_scopes`、`ask_conversations`、`ask_turns` |
| 外部系统接入 | `document_scope_keys`、`personal_access_tokens.workspace_id`、`exploration_grants`（见 [外部系统接入](EXTERNAL_CLIENT_ACCESS.md)） |

`documents`、`document_scope_keys`、`categories`、`tags`、`knowledge_scopes`、`webdav_sources`、
`external_item_exclusions` 和 `ask_conversations` 直接携带 `workspace_id`；其版本、切片、
数据集和证据通过父对象继承空间边界。模型配置、向量 Profile、备份和管理员仍是全局资源。

Scope Key 是文档分组而不是用户/角色系统；`document_selection` 以 SQL
`EXISTS(scope_key IN ...) OR Document.id IN (...)` 形成候选并集，再与 KnowledgeScope
及其他元数据筛选求交。普通 PAT 下它是查询条件；短期知识探索凭证把同一选择固化为
服务端授权边界，Skill REST 与 MCP 的所有搜索、问答、数据集、文档、切片和证据读取
均额外与边界取交集。
探索凭证只存 SHA-256 哈希，绑定工作空间和创建 PAT，最长 24 小时。知识图谱、团队空间和多用户权限仍属于路线图，
不作为当前部署依赖。

## 9. 安全边界

- 当前为数据库约束的单管理员系统；密码使用 scrypt，服务端会话只存 token SHA-256。
- AI Key、Embedding Key 和 WebDAV 密码使用 Fernet 加密；主密钥来自
  `CANGZHI_SECRET_KEY`，未设置时生成到 `storage/.secret_key`。
- PAT 明文只显示一次，按 `read/search/ask/documents:write` 最小权限授权，可选绑定单一
  工作空间，可撤销后删除。
- 短期知识探索凭证明文只返回一次，只能访问创建时固化的文档范围；到期、主动撤销或
  创建 PAT 失效都会使其失效。普通 PAT 直接调用 MCP 的原有方式继续保留。
- URL 抓取限制协议、地址、重定向、响应大小和超时；访问可信内网 WebDAV 需要显式
  启用对应选项。
- WebDAV 始终只读；远端删除经过连续成功扫描和保护期确认，默认只移入本地回收站。
- HTML 预览不执行来源脚本；上传和表格解析有大小、解压与单元格数量边界。
- 公网部署必须在反向代理终止 TLS，并只开放必要端口。

## 10. 故障和一致性语义

- `migrate` 成功后 API 才启动，避免新代码运行在旧 Schema 上。
- Worker 任务可重试且幂等；中途退出不会把半成品标记为当前版本。
- 新向量 Profile 和新 Parquet 产物构建完成前，旧的活动版本继续服务。
- 向量不可用时全文检索继续工作；对话模型不可用时采集与知识管理继续工作。
- 删除连接器、远端删除、删除知识和永久删除是四种不同操作，不相互隐式扩大。
- 备份必须同时包含 PostgreSQL 与 `storage/`；只备份其一不能完整恢复。

## 11. 当前容量边界与演进

当前架构面向单用户、单机和个人规模，优先减少 Redis、Elasticsearch、专用向量库
等组件。达到以下条件再评估拆分：

- 当前切片或向量达到十万级且精确向量查询成为持续瓶颈；
- 数据集并发计算需要独立资源池；
- 多用户导致权限、隔离和审计成为核心复杂度；
- 数据库任务队列无法满足吞吐或调度要求；
- 本地存储容量、可靠性或多节点共享要求引入 S3/MinIO。

任何演进都不能改变“原文与版本是事实源、派生数据可重建、证据可追溯”的基础约束。

## 12. 验证策略

- 单元测试：解析、切片、检索融合、数据集计划、权限与 URL 安全。
- API 集成测试：采集、任务、搜索、问答、引用、WebDAV 与向量生命周期。
- 检索黄金集：标注正确文档、片段和数据行，分别评测全文、向量和混合 Top K。
- 浏览器验证：桌面与手机的核心采集、管理、搜索、问答和证据查看流程。
- 运维验证：Compose 健康检查、备份校验和隔离恢复演练。
