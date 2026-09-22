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

Worker 采用分阶段公平轮转：为解析（含 stored）、切片、数据集目录/产物、预览、
AI 理解和向量保留领取机会，未知阶段也有兜底槽位。各槽位内部按创建时间/id 排序，
只领取到期的 created/retry 任务，使用 PostgreSQL `FOR UPDATE SKIP LOCKED`。
每次准备执行时才领取一个任务，每轮最多尝试 10 个任务（失败也占用预算），
不再提前将十个待执行任务全标成 processing。空槽位跳过，队列空时结束轮次。

这是调度公平性，不是独立并行执行池或耗时保证：OCR、大表、预览和模型调用仍可能
耗时，当前执行的任务不会被抢占。当前仍保留单 Worker 串行执行，避免同版本重解析
和切片替换并发；心跳、租约、超时恢复与安全并行需继续完成 CZ-Q05，不能直接批量
重置已有 processing 任务。修改源码后须由维护者更新运行 Worker 才能生效。

## 5. 双引擎知识模型

### 5.1 文档引擎

PDF、Word、Markdown、网页和随手记按章节、条款、段落、列表与表格边界解析。
`document_chunks` 保存两种当前切片：

- `child`：用于关键词和向量召回；
- `parent`：用于补充完整上下文。

切片保留 `heading_path`、页码、段落号和 `source_start/source_end`。过长自然段先按
句子边界切分，必要时才硬切，并携带有限重叠，降低信息在边界处被截断的风险。

结构正确性属于基础能力，不依赖知识增强。PDF 在生成在线切片前用当前确定性规则重新
校验标题；旧解析器误标的标题只在派生视图中降级为正文，版本化解析事实不变。系统保存
短片段比例、章节密度和重复标题等有界质量诊断；质量严重异常时自动退回按页保持顺序的
安全切片。文档详情可分页读取当前在线 `child` 切片，读取不调用模型，也不修改索引。
AI 边界建议仍是高级候选，必须经覆盖与顺序校验，不能成为基础入库成功的前置条件。

原文件始终可下载。浏览时优先使用原生 PDF；Word 等格式可生成一次性 PDF 预览，
预览不是事实源。Markdown 使用安全渲染；引用通过文档版本和切片位置回到原文。

DOCX 在 `v1.0` 后的 CZ-Q06 改为按正文直接子节点 `w:p` / `w:tbl` 原序
解析，而非先段落后表格；`paragraph_index` 与 `Block.extra.docx_body_index`
均记录两类节点合计的零基位置（空段落计数但不输出，`w:sectPr` 不计数）。
`metadata.docx_extraction` 标记解析器版本、索引语义与能力限制。
标题路径按真实标题级别维护栈，支持跳级、同级替换与从二/三级开始，不虚构缺失祖先。
DOC 转换为 DOCX 后复用相同逻辑。此位置不是 PDF 页码或 Word 的段落专用序号。
嵌套表格、内容控件/修订包装、合并单元格的完整结构仍未支持；不自动重跑历史版本。
分层 AI 理解与文档地图仍为[后续计划](DOCUMENT_UNDERSTANDING_PLAN.md)，不是本批能力。

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

### 5.1.2 外部视觉 OCR（OCR 第二阶段）

本地 tesseract 是必选通道；当扫描页本地识别失败、识别字符数低于阈值或
平均置信度低于阈值时，Worker 可选地把同一张渲染后的 PNG 发送给一个
OpenAI 兼容的多模态模型，由模型返回归一化坐标中的行级文本与置信度。
外部渠道是独立于对话和向量渠道的第三套配置：

- 入口在 `/settings?section=ocr`，面板名为「图片文字识别」，与对话
  面板并列；可勾选「复用对话渠道的地址和密钥」一键填充地址与密钥，
  模型名仍独立指定。
- 触发条件按以下顺序短路：本地结果 `local_chars ≥ ocr_min_chars` 且
  `local_confidence ≥ ocr_confidence_threshold/1000` 时不调用；
  `no_text` / `low_chars` / `low_confidence` 三类原因记入
  `metadata.pdf_extraction.external_trigger_reasons`。
- 每次外部调用对应一份 0..1000 顶左坐标的 bbox 响应；Parser 按页面
  真实宽高还原为 PDF 点坐标并翻转 Y 轴后写入 `Block.extra.bbox`，与本地
  tesseract 通道共用同一个坐标系。
- 单文档外发页数受 `ocr_max_external_pages` 限制（0 表示关闭外部
  渠道），超过后剩余候选页继续走本地识别并标记
  `external_skipped_pages` 与触发原因 `max_external_pages_reached`。
- 外部调用失败（`auth` / `network` / `timeout` / `upstream` / `invalid`
  类别）一律保留本地非空结果，整份文档解析不会因此失败；
  `Block.extra` 同时记录 `external_used: false` 与
  `external_status` 以便文档详情页区分「本地命中」「外部完成」「外部
  失败后回退」「外发超限」等情形。
- `Block.extra` 保留历史 `source=ocr` 和 `ocr_engine`，并增加
  `{ocr_source, engine, provider, model, bbox, confidence,
  external_used, external_status}`；`ocr_source` 取值 `local` /
  `external`，`engine` 取值 `tesseract` / `external-vision`，避免破坏
  既有切片和检索消费方。
- `metadata.pdf_extraction` 在保留全部旧字段的前提下增加
  `external_provider { provider, model }`、`external_attempted_pages`、
  `external_completed_pages`、`external_failed_pages`、
  `external_skipped_pages` 与 `external_trigger_reasons`，全部
  不含密钥与图片字节；Provider 也不会把它们写日志。
- Provider 通过 `apps/api/ocr/` 中的抽象基类注入；首个内置实现是
  `OpenAICompatibleOcrProvider`，它把 PNG 编码成 `data:image/png;
  base64,...` 走 `/chat/completions`，提示词只取回严格 JSON 行列表
  并对坐标做 0..1000 夹紧；后续可加入 `OllamaVisionOcrProvider` 等
  同协议实现而无需修改 Worker 或 Parser。

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

`dataset_fields` 同时保留两层信息：类型、空值、唯一值、范围和样例是程序
画像；字段说明、单位、别名和置信度是可选的 AI 派生建议。新数据集在列式
产物就绪后以低优先级批量尝试补充，历史数据集可由管理页主动生成。模型输出
必须精确匹配已有字段名并通过长度、类型和数量校验；不能覆盖程序画像或修改数据事实。
快速问答与外部 Schema 读取可使用这些语义建议做自然语言映射，但受控查询计划仍只接受原始字段名。

Excel 不能仅凭文件类型视为关系型数据。新解析器对显式结构风险标记
`dataset_eligible=false`：合并、并排、说明/汇总混排或未确认表头的区域保留原始
行列和诊断，但不生成普通全文切片、向量或可聚合数据集，页面提示先治理。同一工作簿
可以同时包含可查询数据集与待治理区域。标题行、第二行表头和常见合并表头等通用形态
可由规则或 AI 提议映射，只有确定性校验通过后才进入数据集。XLSX 依据实际值与公式
边界排除纯样式空白，但仍保留资源保护。适用范围与局限见
[Excel 解析说明](SPREADSHEET_PARSING.md)，发布状态以 PROJECT_STATUS 为准。

外部 PostgreSQL/MySQL 也通过同一数据集引擎进入藏知。连接器只接受分离的主机、端口、
库名和凭据，不接受 DSN 或任意 SQL；运行时先从 `information_schema` 取得 Schema、表和
字段，再由用户选择整表/视图创建本地快照。远端会话强制只读并设置连接与语句超时，
密码加密保存，来源与快照按工作空间隔离。当前单表上限为 10 万行；更大的数据应先在
来源库建立只读视图，后续版本再增加增量同步与可视化筛选导入。

导入后，行数据进入 `structured_table_rows`，同时生成 Parquet 供 DuckDB 执行；
`DocumentVersion` 只保留字段和快照摘要，避免为大表再复制一份完整 JSON/文本。
重导入沿用同一文档并产生新版本，新 Parquet 提交成功后才清理旧产物。

数据库快照有显式新鲜度策略。默认 `background` 在超过间隔后仍以当前
`document_version_id + artifact_version` 完成本次查询，并排入去重刷新任务；`strict`
先排刷新并拒绝执行旧快照；`manual` 只提示管理员重新导入。目录、Schema 与查询结果
统一返回快照时间、年龄、过期和任务状态，REST/MCP/问答复用同一服务层。刷新失败进入
退避重试并保留旧快照；系统不会为追求“实时”让模型向远端执行任意 SQL。完整取舍见
[ADR-028](ADR-028-database-snapshot-freshness.md)。

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

文档级排名与证据完整性分开处理：每路为入选文档暂存最多8个已授权候选，利用有界
正文查询特征和标题降权选择至多1个补充锚点。主chunk、排名、分页、context均不变；
`supporting_evidence` 独立携带同版本定位和最多1800字上下文，避免用更相似的段落
替掉包含关键细节的原锚点。无额外模型调用、不改索引，读取共用权限和邻接窗口逻辑。
深度分析可观察、读取并分别引用补充片段，既有最终证据/观察预算仍生效。快速问答
独立证据召回未在本批调整；细节见 ADR-025。发布状态以 PROJECT_STATUS 为准。

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
| 身份、空间与模型 | `admins`、`auth_sessions`、`personal_access_tokens`、`workspaces`、`ai_runtime_configs`（含 `ocr_provider` / `ocr_base_url` / `ocr_model` / `ocr_api_key_cipher` / `ocr_confidence_threshold` / `ocr_min_chars` / `ocr_max_external_pages` 等外部 OCR 字段） |
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

## 13. AI 切片候选与在线索引的边界

`chunking_candidate` 是独立纯构建服务，接收结构块和可选模型，返回分组候选与
诊断。模型只输出块编号，不输出替代原文；窗口内顺序、完整覆盖、大小与表格邻接
由程序校验。无法调用、非法输出或预算耗尽时使用确定性规则，并明确记录覆盖范围。
原生 PDF 标题识别不再依赖 `isupper()`；历史错误结构仅在候选副本保守纠正。

浏览器管理员通过 `chunking_preview` 服务主动评估，单次最多 2 个模型窗口，
不在检索或外部 MCP 读取时触发。结果摘要缓存在 `DocumentVersion.meta`，缓存键
包括源结构指纹、模型身份和策略版本；模型调用后重新校验当前版本，防止旧结果污染
新资料。候选样例有明确截断提示；计数是默认配置下重算值，不是当前服务索引计数。

管理页面预览不写入在线 `DocumentChunk`。经维护者明确授权的维护入口可以重建
当前切片并异步补向量，但不提供原子激活和旧引用保留；回退仍依赖联合备份。
原子激活、保留旧引用和回滚需要独立产物版本与真实跨类型评测，见 ADR-023 / CZ-N02。

独立 adaptive-v2 候选按结构风险而非 PDF 扩展名选择模型窗口。维护调度器的
`--adaptive-chunking` 是显式启用开关，只影响截止时间后的新正文任务；不改变
已指定策略的重试任务，不重解析历史 PDF，不扩张历史任务白名单。旧 v1 保持原义。
模型无有效输出时保留完整规则基线。数据集仍走现有目录/精确查询路径；跨页关联、
超长块内部语义切分与分章节渐进任务尚未交付。

管理预览按文档类型选择候选：PDF 保留已验证的 v1 历史误标题修正，其他格式使用
adaptive-v2；缓存指纹包含实际选用策略。尚未完成 PDF 对照评测前不直接替换该路径。
`document_map` 和 `knowledge_enhancement_policy` 已接入第二批独立增强运行服务。
迁移 0032 保存版本快照与逐窗口结果；管理员空间配置默认关闭，开启后的新版本在
解析成功后自动提交独立任务，历史资料需显式发起。基础 AI 整理保持不变。
每任务最多一次模型请求，先提交调用计数与租约，再外发；结果提交时校验版本、
源指纹及尝试 ID。文档锁统一先于运行锁，取消/续跑使迟到响应失效。失败不会写坏
基础解析状态，读取结果不调用模型。详见 ADR-024 与 KNOWLEDGE_ENHANCEMENT。
这一层只补充窗口章节摘要/实体关系/事件，不参与当前召回；跨窗口全局理解和切片
原子激活仍未接入，不应称作完整知识图谱服务。第三批 `enhancement_read` 服务将
已有产物作为只读探索对象提供给 REST/MCP/CLI/Skill，每次返回一个窗口的一种视图。
授权检查先于原文/快照读取，空间、文档选择和探索凭证边界取交集；版本及源指纹
变化拒绝作为当前知识返回。解释和原文证据分别分页，不触发生成或隐式重复计费。

迁移0033 增加独立 `knowledge_enhancement_nodes`。可选 overview 模块要求 chapter，
旧配置不自动升级。源窗口全部完成后，最多四个子摘要归纳一个父节点，逐层直到根；
这是连续窗口汇总树，不冒充语义章节树。每节点至多一次模型调用，复用相同预算、
租约、取消/继续与迟到响应校验，只有叶窗口和全树完成才标记运行 completed。
模型只返回有限摘要及有效子引用；读者沿节点到窗口原文核验，摘要不是原文事实。
REST/MCP/CLI/管理页面共用 overview 读取，节点最多四个子结果；直接子窗口的同名
或别名实体只生成 unresolved 导航线索，不进行全篇实体合并或影响当前检索。

`source_navigation` 提供不依赖增强的原文目录/块读取，REST/MCP/CLI 共用服务。
先以工作空间、文档选择和探索凭证交集验证当前资料，再做 SQL 结构长度预检；正文
查询重复大小谓词，防止预检后并发重解析导致无界载入。当前采用有上限的请求内地图
构建（800万序列化字符、2万块、200万正文字符），不是数据库块级缓存。目录是解析器
标题，全部块保留原序及重复/空块；原文块 ID 固定版本和指纹，变更后需重读地图。
读取不创建缓存、任务或模型调用；路径标签可显式截断，证据正文仅按字符分页，不
伪称为完整表格行。数据集继续专用工具，缺失/超大结构回退既有分页原文读取。

向量 Worker 每任务完成后的进度收敛使用 PostgreSQL 聚合与 `vector_dims`，只返回
计数，不将全库正文或向量浮点数组载入 Python。SQLite 测试路径额外校验 JSON 向量
类型、维度和有限性；只统计已为该 Profile 创建任务的有效子片段，保留数据集抽样语义。

### 历史 PDF 的检索时上下文恢复

显式 `structural-v3` 维护候选可启用 `preserve_source_spans`：长块沿原文本区间
切分，句子和行只提供边界，不重新拼接或推算固定分隔符长度。表格长行延续行范围
元数据，不伪造原文前缀。已有入库/v1/v2 默认不开启此选项；不会自动重建现有切片。
覆盖校验验证实际内容而非字数比例；任何无法核对的片段源位置都会阻止维护替换。
`core_source_*` 对应结构块按双换行连接的文本坐标，overlap 为额外检索上下文；
不可把这些字符偏移当成原文件字节或PDF几何坐标。候选验收仍需真实检索对照。

`fragment_context` 在已通过范围校验的检索命中之后执行，只读取同一文档当前版本的
同一物理页原始结构块。对于至少 8 块、60% 以上是短块、命中片段不超过 256 字符的
碎片化 PDF，在锚点核对通过后恢复完整小页面；最多 512 块 / 4000 字符，不跨页，
不修改或规范化原文，包含结构化表格或代码块时不做整页拼接。数据库侧按页提取，
不下载整份文档结构。超限、缺少原文或不能核对锚点时保留已有邻居上下文策略。

搜索的 `context` 仍遵守原有 1800 字符预算；快速问答会将同页恢复结果去重，并在
总证据预算内优先保留完整页面，避免重复命中摊薄每条证据。深度分析的搜索观察和
内部读取同样使用该恢复逻辑。普通长段落、Word/Markdown 和 Excel/数据库数据集
不启用 PDF 整页恢复。此策略无额外模型调用或用户配置，不重切、不重建向量。
如果总预算不足以保留页面，围绕命中锚点截取并显示省略标记，不从页首盲目截取。

文档版本、页码和 chunk ID 仍指向原检索锚点；上下文并不等于该 chunk 的持久化正文。
公共 `knowledge_get_chunk` 保持原始片段读取契约不变。跨页条款、多栏顺序错误和
超限页面尚需后续文档地图与受控探索，不能把本批次解释为完整 AI 切片上线。
