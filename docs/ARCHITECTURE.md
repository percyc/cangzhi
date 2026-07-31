# 藏知技术架构

## 1. 架构目标

藏知在架构上是“知识事实源 + 可重建理解与索引 + 多入口取证服务”，而不是围绕某个
聊天页面搭建的一次性 RAG 管线。

当前面向个人部署，优先降低组件数量和运维成本，同时保持四项不变量：

1. 原文和文档版本是事实源，模型输出和索引都是可重建派生数据。
2. 采集、解析、检索或模型失败不能导致已经接收的原文丢失。
3. 网页、REST、CLI 和 MCP 复用同一服务层、知识范围和引用语义。
4. 外部来源和模型可以替换，不能成为知识资产的锁定点。

```text
Browser
  │
  ├── Next.js Web
  │      │
  │      └── FastAPI
  │             ├── PostgreSQL + pgvector
  │             ├── Local object storage
  │             ├── Ingestion worker
  │             └── Model providers
  │                    ├── OpenAI-compatible API
  │                    └── Ollama/local model
  │
  └── Search / Ask / Manage

CLI / Hermes / OpenClaw / Other Agents
  │
  └── REST v1 / MCP
             │
             └── Shared knowledge services
```

## 2. 代码目录建议

```text
cangzhi/
├── apps/
│   ├── web/                 # Next.js 前端
│   ├── api/                 # FastAPI 接口
│   └── worker/              # 后台解析与索引 Worker
├── packages/
│   ├── prompts/             # 内置提示词与版本
│   ├── schemas/             # 跨服务 DTO / JSON Schema
│   └── config/              # 共享配置约定
├── migrations/              # Alembic 数据库迁移
├── storage/                 # 本地原文与派生文件，默认不提交
├── tests/
│   ├── fixtures/            # 脱敏测试文档
│   ├── retrieval/           # 检索黄金集
│   └── e2e/
├── docs/
├── compose.yaml
├── .env.example
└── README.md
```

## 3. 核心模块

### 3.1 采集层

统一接受 `url`、`file`、`note` 三类输入，转换为不可变 Source：

- URL：保存 URL、抓取时间、响应元数据、原始 HTML 和正文快照。
- 文件：保存原文件、SHA-256、MIME、大小和上传时间。
- 随手记：保存用户输入的原始 Markdown，不用 AI 结果替换。

所有入口先完成落盘和数据库提交，再异步解析，防止 AI 或解析失败导致资料丢失。

### 3.2 解析层

解析器采用路由机制：

1. 根据 MIME 和文件签名选择解析器，不能只依赖扩展名。
2. 优先提取原生文本和结构。
3. 无文本或文本质量过低时触发 OCR。
4. 输出统一的结构树，而不是直接输出切片。

统一结构节点至少包含：

```json
{
  "type": "heading|paragraph|list|table|image|page|article",
  "text": "...",
  "level": 2,
  "page": 5,
  "path": ["第三章", "3.2 检索"],
  "source_span": {"start": 1200, "end": 1680}
}
```

### 3.3 理解层

采用可版本化的处理 Profile：

- `general_article`
- `personal_note`
- `legal_document`
- `contract`
- `research_paper`

每个 Profile 指定：

- 文档类型判定条件
- 结构解析策略
- 元数据提取 Schema
- 切片策略
- 分类提示词
- 摘要提示词
- 使用的模型能力等级

所有 AI 输出必须经过 JSON Schema 校验。失败时保留原始响应并进入可重试状态。

### 3.4 切片层

首版使用“结构优先的父子切片”：

- Parent：一个完整章节、条款或较长段落，用于回答时补充上下文。
- Child：约 300～600 tokens，用于召回。
- Overlap：只在缺乏自然边界时使用，默认 60 tokens。
- 标题路径和文档摘要作为 contextual prefix 参与索引，但不冒充原文。

边界优先级：

1. 法律条款、标题、章节
2. 段落、列表、表格整体
3. 句子边界
4. token 上限硬切

Excel 等二维资料使用专用规则：先按工作表和空行识别数据区域，再保守识别表头；
每个检索行都写成带列名的自描述文本，并携带 `sheet_name`、`region_index`、
`row_start`、`row_end` 和 `column_names`。表格切片只能在完整行之间分割，不使用
字符级 overlap。百分比、前导零编号和金额等同时保留显示值与原始值。
单工作表最多 150,000 行，同时受工作簿 2,000,000 个单元格、250 MiB
解压大小和单元格长度的分层上限保护。超限是确定性错误，直接失败并展示
具体限制，不进入无意义的自动重试。
大型结构化表保留全量原文、全文子切片和逐行结构化索引；向量层仅对均匀分布的
代表性切片抽样，避免为数万个高度同构的表格切片重复调用外部向量服务。
具体值筛选与统计仍由全量行索引执行，不依赖向量抽样。

同一批表格行还会写入独立的 `structured_table_rows` 索引，并在
`knowledge_datasets`、`dataset_fields` 中登记工作表区域、字段类型、语义角色、
空值、唯一值、样例和质量画像。工作簿属于统一文档生命周期，但数据预览与查询走
数据集 API，不渲染超长文本正文。

统计型问题由模型生成
严格受限的查询计划，再由服务端执行白名单筛选、分组和聚合；系统不会执行模型
生成的 SQL 或代码。精确计算无法安全完成时，问答流程才回退到普通语义检索。

表格路由不以“有什么”等单一关键词为准。入库理解阶段生成的文档摘要与
表名、列名、行数和样例共同构成表格语义画像。只有当问题能映射到字段、
字面筛选条件完整，或相关性规划器明确返回 `relevant=true` 时，才进入
结构化查询；否则保持普通 RAG 路径。

当前逐行 JSON 索引是数据集引擎的第一阶段。后续全量数据迁移到 Parquet 等列式
派生文件，由 DuckDB 执行列裁剪、条件下推、聚合和分页；PostgreSQL继续保存知识
对象、数据集目录、字段画像、版本和任务状态。向量只索引数据集、字段和代表行，
不承担全量事实计算。

用户修改片段时，新建 chunk revision，并使旧索引失效；不直接覆盖历史记录。

### 3.5 检索层

首版不单独部署 Elasticsearch，使用 PostgreSQL 完成：

- `tsvector` 全文索引
- pgvector HNSW 向量索引
- 分类、标签、时间和来源过滤
- RRF 合并全文与向量排名
- 可选跨编码器或 API Reranker 精排前 30 条

问答检索流程：

```text
query
  -> 意图与过滤条件提取
  -> query embedding
  -> BM25/FTS top 30 + vector top 30
  -> RRF 融合
  -> rerank top 20
  -> 相邻片段与 parent 扩展
  -> 去重和 token budget 裁剪
  -> LLM 生成
  -> 引用校验
```

回答规则：检索证据不足时必须明确说明，不允许用模型常识伪装成知识库内容。
涉及表格求和、计数、分组、排序等全量计算时，普通 RAG 不得冒充精确计算；后续由
结构化查询通道执行并返回工作表及行范围引用。

### 3.6 知识服务与接入层

搜索、文档读取、知识范围解析、问答和引用校验由 API 服务层统一实现。浏览器业务
接口、REST v1、CLI 和 MCP 只是不同适配器，不各自实现检索 SQL 或模型调用。

```text
Browser API ─┐
REST v1 ─────┼─> KnowledgeScope -> retrieval/read/ask services -> evidence
CLI ─────────┤
MCP ─────────┘
```

外部 Agent 默认使用只读取证能力：

- 列出可用知识范围；
- 执行混合检索；
- 读取指定文档或切片；
- 获得结构化定位和原文片段。

只有调用方明确希望由藏知模型回答时，才使用 `knowledge:ask`。这种边界避免 Hermes、
OpenClaw 等已有推理模型的平台重复调用模型，同时让网页和 Agent 获得一致证据。

## 4. 核心数据模型

### 4.1 内容与版本

- `sources`：原始输入及来源类型
- `documents`：用户可见的逻辑资料
- `document_versions`：每次抓取、上传或重新解析的版本
- `blobs`：原文件、网页快照、预览和解析产物
- `structure_nodes`：章节、段落、表格、页面等原文结构
- `chunks`：检索单元和父子关系
- `chunk_revisions`：用户调整历史

### 4.2 组织与语义

- `categories`：邻接表或 materialized path 主分类树
- `document_categories`：主分类及推荐置信度
- `tags` / `document_tags`
- `entities` / `entity_mentions`
- `relations`：个人 Beta 后启用
- `summaries`：文档级、章节级摘要及模型版本

### 4.3 AI 与检索

- `model_providers`：加密保存的 Provider 配置引用
- `model_presets`：不同任务对应的模型
- `processing_profiles`：解析和提取模板
- `prompt_versions`：提示词版本
- `embeddings`：对象、模型、维度、向量、内容哈希
- `processing_jobs`：后台任务、状态、重试和错误
- `citations`：回答句子与 chunk/source span 的对应关系
- `user_corrections`：分类、标签、摘要和切片纠正

### 4.4 外部来源与接入

- `webdav_sources`：加密凭据、扫描范围、远端删除策略和运行状态
- `webdav_entries`：远端稳定身份、同步状态、缺失确认和关联文档
- `external_item_exclusions`：永久删除后保留的外部来源排除记录
- `knowledge_scopes`：网页与外部客户端共享的知识范围
- `personal_access_tokens`：外部客户端令牌摘要、权限、撤销和使用时间

## 5. 后台任务状态机

```text
created
  -> stored
  -> parsing
  -> understanding
  -> chunking
  -> indexing
  -> ready
```

任何阶段可进入 `failed`，并记录：

- 失败阶段
- 可读错误信息
- 技术错误详情
- 重试次数
- 下一次重试时间
- 使用的解析器、模型和提示词版本

任务必须幂等。以 `document_version + stage + config_hash` 作为幂等键，避免重试产生重复切片和向量。

## 6. API 边界

接口分为三类：

1. **浏览器业务 API**：采集、知识管理、分类标签、任务、模型配置、连接器和导出，
   使用管理员 Cookie，允许随产品界面共同演进。
2. **稳定 REST v1**：`/api/v1` 下的能力、知识范围、搜索、问答、文档和切片读取，
   使用管理员 Cookie 或 Bearer PAT；同一主版本只增加兼容字段。
3. **MCP**：`/api/mcp` 下的 Streamable HTTP 取证工具，复用 REST v1 服务与 PAT
   权限，不复制业务逻辑。

CLI 是 REST v1 的薄客户端，不直连数据库。稳定只读契约和接入示例见
[外部接入](INTEGRATIONS.md)及
[ADR-017](ADR-017-knowledge-hub-adapters.md)。

## 7. 安全与隐私

- 默认只监听本机地址；远程部署必须启用登录和 TLS。
- API Key 只在后端使用，数据库保存加密值或密钥引用。
- 禁止将原文、提示词或 Key 写入普通应用日志。
- URL 抓取需阻止访问环回地址、内网地址和云元数据地址，防止 SSRF。
- 上传文件限制大小、类型和解压深度，并预留病毒扫描接口。
- HTML 清洗后再显示，禁止执行原网页脚本。
- 所有知识删除先进入回收站；永久删除只允许从回收站发起，并级联清理版本、切片、向量、任务和分类关系。
- Blob 按内容哈希共享，永久删除后仅清理已经没有任何版本引用的原文。
- WebDAV 远端始终只读。远端文件使用“规范化来源 + 绝对路径”的稳定身份；永久删除会保留外部排除记录，避免连接器重建或重新扫描后自动复活。
- 单次成功扫描未发现文件时只标记为“待确认缺失”；连续成功扫描且超过保护期后才确认远端删除。默认仅把对应知识移入回收站，不自动永久删除；远端文件恢复时自动恢复知识，用户也可以将其保留为独立本地快照。
- 删除 WebDAV 连接器默认保留已入库知识，也可选择移入回收站；停用连接器只暂停扫描同步，不删除配置或知识。

## 8. 可观测性

至少记录：

- 每阶段耗时与成功率
- 单文档 token 和模型费用
- 解析文本长度及 OCR 是否触发
- 分类结果和用户纠正
- 每路检索的命中、最终排名和引用使用情况
- Prompt、模型、Embedding 和索引版本

## 9. 测试策略

- 单元测试：解析路由、结构切片、RRF、权限和 URL 安全。
- 合约测试：每个 AI 输出的 JSON Schema。
- 集成测试：文件入库到索引完成的完整链路。
- 检索评测：固定问题、相关文档、正确片段和引用位置。
- E2E：URL 入库、文件上传、搜索、问答、纠正和重试。
- 恢复测试：从数据库备份与 storage 目录恢复后重新生成索引。
