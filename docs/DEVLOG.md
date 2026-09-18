# 藏知开发日志

> 追加式变更记录。**只在文末追加，不重写、不删除、不调整旧条目**；修正请另
> 起一条并引用旧条目 ID。任何需要追溯或撤销的信息都在这里。
>
> 条目格式：
>
> ```text
> ### YYYY-MM-DD 标题（关联 ID / 里程碑）
> - 背景：为什么要做。
> - 变更：实际改了什么，列出关键文件与模块。
> - 理由：当时怎么权衡的；如推翻旧决定请引用旧条目。
> - 测试：覆盖了哪些单元 / 集成 / 黄金集；如未覆盖请说明原因。
> - 部署：是否需要镜像重建、迁移、配置变更。
> - 后续：下一步或遗留事项。
> ```
>
> 阶段与里程碑归属写 `ROADMAP.md`；未完成任务写 `BACKLOG.md`；技术取舍写
> `ADR-*.md` 并在 `DECISIONS.md` 登记。本文件不复述这些。

## 模板

复制下列模板追加到本文件末尾，填写所有字段；空字段写 `n/a` 而不是删除行，
保证历史格式一致。

```text
### YYYY-MM-DD 标题（CZ-XX / 基线 X / ADR-NNN）
- 背景：
- 变更：
- 理由：
- 测试：
- 部署：
- 后续：
```

## 索引

- 2026-08-31 外部视觉 OCR 第二阶段实现（CZ-D01b / 基线 H）
- 2026-08-30 处理中心与文档详情阶段口径统一（CZ-Q01 / CZ-Q02）
- 2026-08-19 短期知识探索凭证与文档 Scope Key 重命名（ADR-022 / CZ-U02）
- 2026-08-19 单管理员多工作空间交付（基线 F / ADR-009）
- 2026-08-13 受控写入 API：上传、`external_id`、`documents:write`（CZ-I03）
- 2026-08-04 数据集 Parquet / DuckDB 执行后端（M4-1）
- 2026-08-31 建立协作文档体系（AGENTS.md / PROJECT_STATUS.md / DEVLOG.md）

## 变更记录

### 2026-08-31 外部视觉 OCR 第二阶段实现（CZ-D01b / 基线 H）

- 背景：基线 H 第一阶段已交付扫描 PDF 文字兜底（本地 tesseract）。个人资料
  中仍有扫描质量差、专业排版或公式密集的页面，本地识别字符或置信度不足，
  需要可插拔的第二条 OCR 通道；引入外部模型不能放大失败面、不能把密钥与
  图片字节写进日志、不能破坏现有 `Block.extra` 与 `metadata.pdf_extraction`
  字段语义。
- 变更：
  - 新增 `apps/api/ocr/`（Provider 抽象基类、`OpenAICompatibleOcrProvider`、
    工厂），Worker 通过 `ai_runtime_configs` 注入 `ExternalOcrProvider`。
  - `apps/api/parsers/pdf.py` 与 `apps/worker/services/processor.py` 接入触
    发条件 `no_text` / `low_chars` / `low_confidence`、单文档外发页数限制
    与超时回退。
  - `Block.extra` 与 `metadata.pdf_extraction` 扩展本地/外部来源、引擎、
    provider、模型、bbox、置信度、外部触发原因与外发跳过原因，旧字段保持
    兼容。
  - 新增设置页“图片文字识别”面板（`/settings?section=ocr`），可一键复用
    对话渠道的地址与密钥，模型名仍独立指定。
  - 新增 `apps/api/tests/test_pdf_external_ocr.py`、
    `apps/api/tests/test_ocr_provider.py`、
    `apps/worker/tests/test_ocr_runtime.py`。
  - 迁移：`migrations/versions/0031_add_ai_runtime_ocr_config.py`。
- 理由：本地 tesseract 始终是必选通道，外部仅在本地不达标时触发；外部失败
  必须保留本地非空结果；外发页数到达上限后剩余候选页继续走本地识别并标记
  `external_skipped_pages`。坐标统一到 PDF 点坐标（Y 轴向上）后与本地
  通道共用坐标系，避免对既有切片与检索消费方造成破坏。
- 测试：`pytest apps/api/tests/test_pdf_external_ocr.py
  apps/api/tests/test_ocr_provider.py apps/worker/tests/test_ocr_runtime.py`。
  通过伪造外部 Provider 验证触发条件、bbox 还原、字段兼容与降级路径；
  本地识别仍用真 tesseract 跑 `tests/api` 已有 PDF 样本。
- 部署：API / Worker 镜像需重建；执行 `alembic upgrade head` 应用
  `0031_add_ai_runtime_ocr_config`；`.env` 无需新增变量。
- 后续：黄金集评测脚本（本地 vs 外部 vs 组合 Top-K 命中率、失败模式），
  并在处理中心与文档详情页暴露统计；外发页数阈值与触发条件从经验值转为
  评测驱动的默认值。

### 2026-08-30 处理中心与文档详情阶段口径统一（CZ-Q01 / CZ-Q02）

- 背景：知识库与处理中心此前对“是否已解析 / 是否已切片 / 是否已建立向量”
  各自维护一份判断口径，文档详情再写一份，导致用户在不同页面看到的状态
  不一致；向量覆盖也按旧 profile 计算，无法兼容大型数据集的抽样向量策略。
- 变更：
  - `apps/api/services/processing_status.py` 统一阶段判定为单一服务入口，
    文档列表、处理中心、详情页均调用同一函数。
  - 向量覆盖按当前 `embedding_profiles`、切片内容哈希与 `dataset_artifacts`
    的抽样策略计算，结果写入 `processing_status.embedding`。
  - 增加“缺失/失败向量”筛选与定向批量补建接口，避免重复下载、解析、切片。
  - 新增 `apps/api/tests/test_processing_status.py` 覆盖阶段口径与定向补建。
- 理由：把阶段判断从视图层收到服务层，未来扩展 WebDAV 定时扫描、网页抓取
  恢复时也只在一处改判定规则。
- 测试：`pytest apps/api/tests/test_processing_status.py`；手工验证文档
  列表 / 处理中心 / 详情三处状态一致。
- 部署：API 镜像重建；无新迁移。
- 后续：`CZ-Q03` 任务统计（耗时、失败类型、重试次数、积压）、`CZ-Q04`
  大文档回归集。

### 2026-08-19 短期知识探索凭证与文档 Scope Key 重命名（ADR-022 / CZ-U02）

- 背景：普通 PAT 在 MCP 上下文中需要把第三方系统本次选定的文档集合固化
  为服务端授权边界，而 REST 端的“文档选择”只是查询条件。直接把 PAT
  交给 Skill 客户端会扩大权限，引入“探索凭证”把选择边界交给服务端强制。
- 变更：
  - `apps/api/security/` 与 `apps/api/services/scope_keys.py` 实现
    `exploration_grants`（`cz_eg_...`）签发、撤销与校验；明文只显示一次，
    服务端只存 SHA-256。
  - `apps/api/services/workspaces.py` 与 `apps/api/api/` 在 REST 写接口
    与 MCP 工具入口强制与探索凭证边界取交集。
  - `migrations/versions/0029_rename_document_scope_keys.py` 文档分组由
    `document_access_keys` 重命名为 `document_scope_keys`，对应模型、
    API 字段、CLI 参数与 Skill 文档同步更新。
  - `migrations/versions/0030_add_exploration_grants.py` 新增
    `exploration_grants` 表与索引。
- 理由：选择边界必须在服务端强制；普通 PAT 的工作空间级 MCP 路径继续保留，
  探索凭证是新一层可选能力，避免一次决定两种权限模型。
- 测试：API 集成测试覆盖签发、撤销、过期、跨空间请求、越权选择；
  MCP 工具集成测试覆盖边界交集。
- 部署：API 镜像重建；执行两次迁移；普通 PAT 仍可继续使用。
- 后续：把探索凭证使用统计纳入 `CZ-O04` 安全审计；写入路径仍只走
  `documents:write`，不扩展到 MCP。

### 2026-08-19 单管理员多工作空间交付（基线 F / ADR-009）

- 背景：单管理员账号下需要把“工作 / 个人 / 专题研究”分成彼此隔离的知识
  上下文；模型渠道、备份与系统运维仍由管理员统一维护。
- 变更：
  - `apps/api/services/workspaces.py` 与 `apps/api/api/` 暴露创建、切换、
    归档与恢复工作空间接口；浏览器用 `cangzhi_workspace` Cookie，REST /
    CLI / MCP / Skill 用 `X-Cangzhi-Workspace` 请求头。
  - `apps/api/models/auth.py` 等模型在 `documents`、`document_scope_keys`、
    `categories`、`tags`、`knowledge_scopes`、`webdav_sources`、
    `external_item_exclusions`、`ask_conversations` 携带 `workspace_id`。
  - `apps/worker/services/processor.py` 处理任务按所属文档重新绑定空间。
  - `migrations/versions/0026_add_workspaces.py`。
- 理由：工作空间是知识隔离边界，不是租户或角色；不存在的空间返回 404，
  已归档空间返回 409，避免被复用为权限开关。
- 测试：API 集成测试覆盖 Cookie / 请求头 / 查询参数 / 默认空间解析顺序，
  以及归档后写入被拒；Worker 任务串库回归测试。
- 部署：API / Worker 镜像重建；执行迁移；历史数据自动归入 `default`。
- 后续：未来若进入多用户，必须独立设计成员关系、角色、空间授权与 PAT 空
  间授权，**不**把当前管理员账号复制成多账号（见 `BACKLOG.md` 备注）。

### 2026-08-13 受控写入 API：上传、`external_id`、`documents:write`（CZ-I03）

- 背景：外部系统需要稳定地把文件送进藏知并维持版本关系；既不能暴露
  MCP 写权限，也不能让上传变成无审计 / 无幂等的入口。
- 变更：
  - `apps/api/api/upload.py` 与 `apps/api/services/document_lifecycle.py`
    暴露 `POST /api/upload`，以 `external_id` 幂等版本更新，返回处理状态
    查询地址。
  - `apps/api/security/` 新增 `documents:write` 权限；PAT 创建时可单独
    授权，与 `knowledge:read` / `knowledge:search` / `knowledge:ask`
    解耦。
  - `apps/api/services/scope_keys.py` 文档 Scope Key 支持单篇与批量整体
    同步。
  - `migrations/versions/0028_add_document_access_keys.py`。
- 理由：写入权限不能从读取权限自动升级；`external_id` 必须由调用方提供
  才能跨请求幂等；处理状态可查询后才能谈重试与一致性。
- 测试：API 集成测试覆盖上传、版本递增、`external_id` 重复提交返回同版本、
  状态查询、Scope Key 单篇 / 批量管理；权限缺失返回 403。
- 部署：API 镜像重建；执行迁移；`.env` 无需新增变量。
- 后续：URL / 随手记稳定 v1 写入接口、`Idempotency-Key`、写入审计、速率
  限制与大小限制（先 ADR 后代码）。

### 2026-08-04 数据集 Parquet / DuckDB 执行后端（M4-1）

- 背景：此前 Excel / 表格查询走轻量内置执行器，能力覆盖有限；大型数据集
  不为每一行做切片与向量，但回答时仍需精确筛选、聚合、排序。
- 变更：
  - `apps/api/services/dataset_execution.py` 接入 DuckDB 与 Parquet，单
    数据集筛选、投影、排序、分组、计数、去重计数、求和、平均、最值下推。
  - `apps/api/services/structured_table.py` 维护可重建 Parquet 产物版本，
    新版本原子切换，旧版本在提交后清理。
  - `migrations/versions/0022_add_dataset_artifacts.py`。
  - 受控查询计划：API 只接受白名单 plan，不接受任意 SQL；内存、线程、
    超时、分页与最大返回行数统一在服务层设边界。
- 理由：把“是否精确”还给执行器，把“是否相关”留给向量；个人规模不必
  立刻上独立计算资源池，先把执行边界与原子切换做好。
- 测试：单数据集筛选聚合下推、计划白名单拒绝、并发隔离、原子切换与
  回滚、Parquet 损坏恢复。
- 部署：API 镜像重建；执行迁移；首次启用会自动从已有结构化数据生成
  Parquet 产物。
- 后续：多表关联、透视、复杂日期函数与跨数据集语义关系（不在个人 Beta
  范围内，留在 `BACKLOG.md`）。

### 2026-08-31 建立协作文档体系（AGENTS.md / PROJECT_STATUS.md / DEVLOG.md）

- 背景：单人维护的代码库开始有多个 Agent（含自动化 Agent 与外部 Skill）
  同时进入；缺乏统一阅读路径会导致重复工作、绕开事实源与不可逆操作约束。
- 变更：
  - 新增 `AGENTS.md`：协作者约定、必读顺序、不可触碰清单、角色边界。
  - 新增 `docs/PROJECT_STATUS.md`：当前阶段、进行中、刚完成、风险、验证、
    下一步。
  - 本文件 `docs/DEVLOG.md`：追加式变更记录与近期条目。
  - `README.md` 文档导航补三条入口。
- 理由：`AGENTS.md` 是入口，`PROJECT_STATUS.md` 是快照，`DEVLOG.md` 是
  历史；不与 `ARCHITECTURE` / `ROADMAP` / `BACKLOG` / `DECISIONS` 内容
  重复，避免维护多份事实源。
- 测试：n/a（纯文档）。
- 部署：n/a。
- 后续：每次完成任务后**先追加** `DEVLOG.md`，再视情况更新
  `PROJECT_STATUS.md`；`ROADMAP` / `BACKLOG` / `DECISIONS` 不混进开发
  日志。

### 2026-08-31 数据集查询补齐外部证据身份（ADR-017 / 基线 E）

- 背景：`knowledge_query_dataset` 虽返回 `dataset_id`、`document_id`、产物版本和
  `source_rows`，但缺少 `document_version_id`、文档标题与规范化查询计划；外部
  DSH 插件无法安全打开回答当时的贡献行，只能退回当前数据表预览。
- 变更：`execute_dataset_query` 的 DuckDB 与 PostgreSQL fallback 响应均增加
  `document_version_id`、`title` 和 `query_plan`。`query_plan` 来自服务端完成白名单
  校验后的 `SafeDatasetQuery`，不返回内部 SQL；其余已有字段含义不变。
- 理由：这是 ADR-017 v1 契约允许的可选字段追加，使外部客户端能够组合
  `document_version_id + dataset_id + artifact_version + source_rows` 调用既有证据
  接口，不引入第二套证据协议。
- 测试：`apps/api/tests/test_dataset_execution.py` 增加 DuckDB 与 fallback 两条
  身份契约断言；完整文件 9 项用例通过（另有 1 条依赖弃用警告）。
- 部署：API / MCP 进程需使用新代码重启或重建镜像；无数据库迁移、无配置变化。
- 后续：由 DSH 插件完成真实会话端到端点击验收；旧工具结果缺版本身份时不得冒用
  当前最新版。

### 2026-09-01 实时处理队列统计（CZ-Q03）

- 背景：`/api/system/status` 只统计当前文档版本的 `processing_status`；文档主体已经
  `ready`、向量等派生任务仍在运行时，管理端会错误显示处理队列为 0。
- 变更：系统状态改为查询当前、未删除文档版本上的 `ProcessingJob`，按文档版本
  去重；`processing` 优先于 `created` / `retry`，并保留没有任务行时的旧字段兼容。
  全局管理员状态查询显式绕过当前工作区过滤，旧版本任务和已删除资料不计入。
- 语义：`active` / `waiting` 表示正在处理或排队的资料数，不是底层任务行数；
  `failed` 与 `failed_by_workspace` 的当前版本失败口径不变。
- 验证：`apps/api/tests/test_system_status.py` 与
  `apps/api/tests/test_processing_status.py` 共 22 项通过；覆盖多向量子任务去重、状态
  优先级、排队、旧版本、软删除、失败版本优先和旧字段兼容。
- 部署：无需迁移和新增配置；API 使用新代码重启后生效。当前任务未执行部署。
- 后续：DSH 独立插件的管理概览仍需在处理期间轮询该接口，完成
  `0 → 处理中 → 0` 的真实浏览器验收。

### 2026-09-09 增加中英文项目说明

- 背景：仓库首页此前只有中文说明，GitHub、Gitee 与自建 Gitea 上的非中文用户
  无法快速了解产品定位、能力边界、部署入口和外部 Agent 接入方式。
- 变更：新增完整英文版 `README.en.md`，覆盖 AI 原生定位、知识生命周期、检索与
  引用、数据集能力、外部接入、技术栈、快速部署、健康检查和开源协议；中文与英文
  README 顶部增加双向语言入口。
- 验证：人工核对两份 README 的章节、命令、端口、路径和文档链接；Markdown 链接
  执行本地存在性检查。
- 部署：n/a（纯文档）；发布时以同一提交同步到项目已配置的 Git 远程。

### 2026-09-14 解析可靠性第一批：Unicode、事务恢复与 OCR 状态

- 背景：解析结果中的 NUL 和孤立 UTF-16 代理字符无法写入 PostgreSQL；Word
  预览捕获 flush 异常后继续访问 ORM，可能用 PendingRollbackError 掩盖原始错误。
  本地 OCR 无文本且外部识别失败或达到页数上限时，页面还可能被计为识别完成。
- 变更：统一规范化派生正文、标题路径和嵌套元数据，移除 NUL、修复有效代理对、
  用替换字符保留无法还原字符的位置；仅记录汇总计数。规范化字段名冲突显式终止，
  避免静默覆盖字段；原始文件与已有来源哈希不变。
- 变更：预览保存点之前显式 flush 正文，正文写入错误交给解析任务 rollback；
  预览自身数据库错误只回滚保存点并清理新建孤立文件。异常日志和持久化诊断保留
  类型，不输出 SQL 参数或转换器原始输出。OCR 回退无有效文本的页面计入失败。
- 协作：OpenCode MiniMax M3 完成初稿；Codex 审阅后补齐字段冲突、事务边界、
  错误脱敏与真实数据库约束回归测试。
- 验证：`pytest` 运行 `test_text_sanitize.py`、`test_parsers.py`、
  `test_pdf_external_ocr.py`、`test_processing_status.py` 与完整 `apps/worker/tests`
  共 155 项通过；另以合成异常字符在 PostgreSQL 只读事务中验证规范化后 JSONB
  转换成功。`git diff --check` 通过；现有 Starlette/httpx 弃用警告不影响结果。
- 边界：本批修复 OCR 页级完成状态，尚未将所有空正文 PDF 改为任务级失败；
  未修改公共 API 契约，无迁移。未部署、未自动重处理历史资料。
- 后续：Worker 心跳/租约与超时恢复、DOCX 段落表格原序分别登记为 CZ-Q05/Q06；
  大型 Excel 限额及混合 PDF 页内图片识别仍需后续评估。

### 2026-09-14 解析可靠性修复主线合并验证

- 按维护者“测试后合并代码”的要求，复核修复提交 `1413786`，并将任务分支
  快进合并至本地 `main`。
- 验证：`.venv/bin/python -m pytest apps/api/tests apps/worker/tests -q`，
  653 项通过，耗时 78.22 秒；4 条既有 Starlette/httpx/cookie 弃用警告。
  `git diff --check` 通过。无前端改动，未重跑前端检查。
- 范围：本地提交与合并；未推送远端，未部署，未修改生产数据。

### 2026-09-14 收件箱大空间加载与轮询修复（CZ-Q07）

- 诊断：环评空间 402 份资料、27,580 个当前子切片，当前档案已有 22,841 个
  向量，向量存储约 179 MB。旧收件箱全量下载后浏览器分页，并每 3 秒重新加载；
  状态查询读取完整文档、切片和向量，现场 API 占满一个 CPU 核，多条数据库查询
  等待 ClientWrite，健康检查耗时约 9.9 秒。
- 后端：新增浏览器管理接口 `GET /api/documents/inbox`，在服务层按当前空间
  汇总轻量状态并筛选分页，返回 `items/total/counts/has_processing`。按 100 份
  资料分批读取状态，标题等展示字段只读取当前页；共享状态查询只返回文本存在性、
  必要元数据、切片身份与哈希、向量哈希和任务状态，不返回正文、完整结构、切片
  内容或向量。详情页 OCR 摘要仍保留；收件箱不请求 OCR 摘要。
- 后端：向量与任务查询使用子查询限定切片，避免大文档导致海量绑定参数；状态
  计算从按文档扫描所有向量任务改为直接查找该文档的任务。抽样向量、过期哈希、
  终止失败、工作空间隔离等语义沿用共享状态服务。
- 前端：由 OpenCode（ark 初步检查，MiniMax M3 实现）完成初稿，Codex 审阅补齐
  取消代次、操作期间防轮询、超时重试按钮与分页展示。只请求当前 25 条；上一轮
  完成后才安排下一轮刷新，隐藏页暂停，翻页/切换筛选/离开页面取消旧请求；20 秒
  超时明确提示。批量按钮明确仅作用于本页，不隐式操作未显示资料。
- 验证：完整 API/Worker 656 项通过；新增服务端分页、各筛选统计、跨批次与空间
  隔离、鉴权、实际查询列不包含大字段的回归；Web lint/typecheck 通过。
  隔离 Chromium 在本地 mock API 上通过分页、本页补建、旧响应丢弃、慢请求
  无重叠轮询、隐藏页暂停、超时与手动重试测试。脚本：
  `scripts/test-inbox-browser.cjs`（启动方式见文件顶部，使用本地 Playwright）。
- 实测：候选服务在独立进程、PostgreSQL 只读事务中访问环评空间；全部筛选
  2.289 秒，返回 25 条/21,163 字节；失败筛选 2.004 秒，返回 13 条/11,597 字节。
  这是服务层单次测量，不是已部署网页的端到端延迟保证。
- 边界：无迁移，无 REST v1/MCP 契约变更；统计仍扫描空间内轻量状态，超大空间
  后续评估持久化汇总。未部署、未重启生产服务、未修改资料或后台任务。

### 2026-09-14 收件箱性能修复合并主线（CZ-Q07）

- 按维护者“测试没问题就合并”的要求，将修复提交 `f177867` 从
  `codex/CZ-Q07-inbox-performance` 快进合并至本地 `main`。
- 沿用本批已完成的验证：完整 API/Worker 656 项、最终调整后的针对性测试
  16 项、Web lint/typecheck、隔离 Chromium 交互测试全部通过。合并前工作区
  干净，主线未发生分叉，`git diff --check main...HEAD` 通过；无需重复执行未变更测试。
- 本次仅本地合并与协作记录更新，未推送远端、未部署。

### 2026-09-14 全页面验收与发布准备（CZ-Q08）

- 维护者明确要求“检查所有页面情况，并部署推送”，本次按该授权执行发布，
  不修改 AGENTS.md 中供后续协作者遵循的默认禁止部署约定。
- 隔离 Chromium + SQLite API 完成 52 个桌面/手机页面与状态检查；补充首次
  初始化、已有管理员重定向和真实登录提交，未调用生产模型或修改生产资料。
  验收范围、边界和复现脚本见 `docs/PAGE_REVIEW.md`。
- OpenCode MiniMax M3 按评审结果修改手机收件箱为卡片，保留桌面表格与既有
  业务逻辑；人工复核截图，Web lint/typecheck 通过。生产 API 镜像内完整
  API/Worker 测试 656 项通过；Next.js 生产构建通过。
- `.dockerignore` 新增 `backups/`，防止备份进入构建镜像。部署前创建并校验
  `backups/cangzhi-20260914-145242`，约 583 MB；保留 API/Worker/Web 的
  `rollback-20260914` 本地镜像标签。数据库与代码迁移 head 均为 `0031`。
- Worker 收到 SIGTERM 后完成当前批次并以 0 退出。任务 44367/44368 从
  14:05 UTC 起已停留 processing，早于本次发布；未重置，后续由 CZ-Q05 处理。

### 2026-09-14 发布完成与生产复核（CZ-Q08）

- `0519319` 已合并并推送 origin/main；使用本地基础镜像缓存重新构建 API、
  Worker、Web、migrate，执行 Compose 更新并等待健康。最终运行源码包含前两批
  解析与收件箱修复，以及手机收件箱卡片；本条仅追加发布记录，无运行代码变化。
- 数据库迁移正常退出，schema 保持 `0031`。API、Web、Worker、PostgreSQL
  全部 healthy，API/Worker/Web 关键文件哈希与源码一致；镜像中无 backups 目录。
- API readiness 200/约 5 ms，Web health 200/约 17 ms，未认证收件箱 API 401；
  实际地址的浏览器登录页与受保护页面转登录通过。未使用或生成生产管理员会话。
- 真实环评空间在新 API 容器的只读服务层复测：402 份资料，首 25 条 2.128 秒，
  21,163 字节。页面数据未全量返回，测试未改动生产资料或任务。
- 隔离 API、开发 Web 与测试浏览器均已关闭；临时页面截图留作验收证据。
  备份与回滚镜像保留，后续维护者可按备份策略轮换。

### 2026-09-14 v1.0 基线与文档理解演进启动

- 按维护者要求，将当前已验证提交 `8cccfb9` 标记为附注标签 `v1.0`，推送到
  origin；未配置 GitHub 远端，不创建 GitHub Release，不自动推送其他远端。
- 创建 `codex/CZ-Q06-document-structure`，先修 Word 原序与章节归属；
  后续文档地图、AI 理解、受约束语义切片及统一探索见 DOCUMENT_UNDERSTANDING_PLAN.md。
- 本轮不部署、不修改生产资料、不自动重跑历史解析；公开契约改变前另写 ADR。

### 2026-09-15 DOCX 原序与章节归属（CZ-Q06）

- OpenCode MiniMax M3 产出解析与测试初稿；Codex 审阅后改用兼容旧版
  python-docx 的直接子节点遍历，修正测试预期，补齐旧依赖与原序上下文回归。
- DOCX 段落与表格按正文原序输出；表格继承出现位置的标题路径。空段落占位置
  但不生成文本块，节属性不占位置。新增 docx_body_index 和 docx_extraction
  版本/能力说明，DOC 转换后同步继承。未改变通用切片规则或检索排序。
- 新增 18 项合成回归：交错/连续/纯表格、章节、空段落、异常样式、确定性、
  原文不变、父子切片归属及序列化一致性。旧 v1.0 解析器在内存对照运行两项
  原序/章节测试均失败，新实现通过；未替换工作区文件或生产代码进行对照。
- 验证：Codex 在 /tmp 运行完整 API/Worker 测试，674 项通过（79.34 秒），
  4 条既有弃用警告；针对性解析测试 51 项通过。git diff --check 通过。
- 边界：嵌套表格、内容控件、修订包装与完整单元格结构仍待 CZ-N01；没有新增
  AI 切片、MCP 工具或数据库迁移。代码留在任务分支，未合并/推送开发代码或部署，
  未自动重建历史资料；v1.0 标签保持不动。

### 2026-09-15 CZ-Q06 发布前完善

- 维护者要求“完善后并入正式环境”，本轮据此授权合并与部署；不修改 AGENTS.md
  对后续任务的默认部署边界，也不自动重处理历史资料或切换索引。
- OpenCode ark-code-latest 补齐 DOCX 标题真实级别栈，修复跳级后回退、同级替换
  与起始标题非一级时的路径串接；Codex 补充正文继承与位置断言。
- 新增 6 项标题回归，针对性解析测试 57 项通过；未改变切片算法、模型提示词或
  公共契约，无数据库迁移。本批与之前 M3 的原序修复一起发布。
- 发布前备份 backups/cangzhi-20260914-160816（路径使用 UTC 时间），数据库与
  storage 校验通过；保留 API/Worker/Web 的 rollback-v1.0-20260915 镜像标签。
- 最终验证：正式 Python 3.11 API 镜像在禁网、无生产数据挂载的隔离容器内运行
  完整 API/Worker 测试，680 项通过（80.92 秒），4 条既有弃用警告。
  运行代码提交 `93f72b8`（含前批 `993e8f2`）；按维护者授权合并发布。

### 2026-09-15 CZ-Q06 正式部署完成

- 任务分支快进合并 main，`79826b3` 已推送 origin。v1.0 附注标签仍指向
  `8cccfb9`，未移动。此后文档提交仅记录验收，不改变已部署运行代码。
- 复用本地基础镜像构建 API/migrate `4b0418deec17`、Worker `e8f4985636c9`；
  旧 Worker 完成当前批次后正常退出（exit 0），随后更新 API/Worker。Web 和
  PostgreSQL 未重建，迁移成功，schema 保持 `0031`。
- `make doctor` 通过：API、Web、Worker、PostgreSQL 全部 healthy；API 与
  Worker 的 DOCX 解析器 SHA-256 与主线源码一致。正式 Worker 中运行内存合成
  DOCX，验证原序、表格注释相邻关系、父片段内容顺序和跳级标题路径通过；未创建
  生产文档或额外调用模型。备份与 rollback-v1.0-20260915 镜像继续保留。
- 新解析逻辑对后续解析任务生效，未自动重跑已处理文档；文档地图、复杂表格结构、
  分层 AI 理解和 MCP/API 新探索工具仍按 CZ-N01–N06 后续交付。

### 2026-09-15 历史 Word 授权重处理（CZ-Q06）

- 维护者要求历史资料开始处理以观察效果。本次只排入未使用新版 DOCX 解析器的
  当前、未删除 Word 文档：默认空间 4 份、AI知识库 56 份、公文模板空间 91 份、
  环评知识库 6 份，共 157 份。不重跑 PDF/Excel，不修改模型或活动索引档案。
- 操作前完成数据库/storage 备份 `backups/cangzhi-20260914-164207`（UTC 命名），
  verify-backup 校验通过。目标资料分类与标签均为模型生成，未发现人工分类或标签。
  原文件不删除；既有同版本重处理会重建派生产物，旧片段引用可能失效，向量未补齐前
  可能退回关键词检索，已向维护者说明。此轮不改变现有重处理流程的这些限制。
- 使用现有 reprocess_document 入口逐份提交，157 份均入队，含成功重新读取原文件的
  2 份 WebDAV 资料。解析任务为 `49562`–`49717` 及复用任务 `16566`。
  未重复重置已在运行的任务，未处理无关的旧向量卡住任务。
- 为验证立即生效，通过行锁及状态检查独占领取文档 428 的待运行解析/切片任务，
  调用现有 Worker 处理函数；没有重复执行其他 Worker 已领取的任务。
  结果：两阶段 completed，parser_version=1，16 个结构块含 1 个表格，正文位置
  索引按原序递增，3 个新片段。初次队列复核：解析完成 2 份、等待 155 份。
- 此时全库仍有 4564 条待运行向量任务，AI 整理/向量完成需继续等待后台队列；
  不将“入队”或“正文解析成功”描述为全部处理完成。本轮没有应用代码变更、迁移
  或部署，没有移动 v1.0 标签；后续分层 AI 理解仍按 CZ-N01–N06 推进。

### 2026-09-15 Worker 公平调度（CZ-Q05 第一批）

- 历史 Word 入队后再次只读核对：仍为 2/157 解析完成、155 等待；全库尚有
  4358 个等待向量任务，最近一小时完成 93 个向量任务。故障是全阶段 FIFO
  队列长期挤占解析机会，并非 Worker 整体停止；旧 completed 派生产物不能当作
  新一轮重处理完成。未重置既有 processing 任务或修改生产队列。
- 负责人 Codex / OpenCode，分支 `codex/CZ-Q05-stage-scheduling`，代码 `7f59ced`。
  MiniMax M3 编写初稿与 14 项测试；ark 跟进调用未产出修改，Codex 接手修复
  默认无界循环、失败计数、空筛选和小预算游标，并增加 6 项回归。
- 各阶段固定轮转，stored/parsing 共用两个 FIFO 槽位，切片、数据集目录/产物、
  预览、理解、向量分别保留槽位，未知阶段有兜底。每个执行槽位仅领取一条到期任务；
  每轮默认最多尝试 10 条，失败也计数，不提前占住十条待执行任务。沿用原有异常
  重试与 Profile 进度收敛逻辑，不修改切片、检索、模型提示词或公共契约。
- 最终 Worker 隔离回归 94 项通过（含 20 项调度测试）。使用运行基线 Python 3.11
  镜像，禁网、只读挂载代码，不挂载生产数据。另建无外部网络、无宿主端口的临时
  PostgreSQL，用两个事务验证锁住的候选被跳过、领取互斥及 processing 不被重复领取；
  验证通过后移除临时容器与其内存测试数据，未影响生产 PostgreSQL。
- 本批是串行公平调度，不是模型调用耗时保证或并行执行池。心跳、租约、异常退出
  恢复与同版本安全并发继续保留 CZ-Q05；不会自动回收遗留 processing 任务。
  按 AGENTS.md 本轮不合并主线、不推送、不部署，维护者更新 Worker 后才生效。
- 最终完整 API/Worker 隔离回归：700 项通过（171.66 秒），4 条既有弃用警告；
  git diff --check 通过。运行中的生产镜像未变，不将代码通过测试描述为线上已恢复。

### 2026-09-15 公平调度正式环境验收启动

- 维护者要求“合并，并在正式环境验收”，本次据此授权更新 Worker；不改变
  AGENTS.md 对后续任务的默认部署边界。确认远端无新增提交后，任务分支快进
  合并 main 并推送 `ff3820b`，运行代码 `7f59ced`，v1.0 标签不移动。
- 发布前备份 `backups/cangzhi-20260915-005955`，数据库/storage/元数据校验通过；
  旧镜像保留为 `cangzhi-worker:rollback-before-stage-scheduling-20260915`。
  新 Worker 镜像 `1b131d18f75d` 内隔离执行 94 项 Worker 测试通过，处理器文件
  SHA-256 与主线一致。本次无迁移，不更新 API、Web 或 PostgreSQL。
- 旧 Worker 收到正常停止请求后完成当前批次，exit 0 退出；随后仅替换 Worker，
  未强制终止或重置旧任务。新 Worker 于 2026-09-15 01:04:28 UTC 开始领取。
  运行处理器 SHA-256 与主线一致，make doctor 全部 healthy，schema 保持 0031。
- 连续多轮正式环境验收：本批解析从 2/157 增至 7/157，新增 5 份全部确认
  metadata.docx_extraction.parser_version=1，150 份仍等待。新 Worker 同时完成
  3 个切片、3 个数据集产物、2 个理解与 1 个向量任务，证明模型与结构阶段均推进。
  不将全库旧 completed 记录纳入本次增量验收；没有声称全部重处理完成。
- 两条上线前遗留 processing 向量任务 `44367` / `44368` 未重置，后续按心跳/租约
  恢复工作处理。备份与回滚镜像保留；调度公平性通过，不代表模型延迟或全库整理完成。

### 2026-09-17 reconcile_profile_progress 改为 SQL 聚合（CZ-Q03 性能前置）

- 背景：reconcile_profile_progress 每次扫描全库 current child chunk 与
  chunk_embeddings 行，再在 Python 里数 completed / failed / pending；CZ-Q03
  报告向量覆盖统计在切片刻增长时全量加载会拖慢处理中心与文档详情；CZ-N04
  AI 辅助切片与黄金集评测之前先把统计端改造完。
- 负责人 Codex / OpenCode，本地未推送、未部署、未触碰公共契约与迁移。
- 改动 apps/worker/services/embedding_processor.py：
  * 新增 `_build_progress_aggregate_stmt` 与 `_aggregate_progress`，
    一次聚合查询拿 total / failed / pending / completed。PostgreSQL
    路径用 `vector_dims(ce.vector)` 服务端校验维度，SQLite 测试路径
    用 `json_array_length(...)`；候选行不再 SELECT vector payload，
    文档切片的 `content` / `search_text` 也不进 Python。
  * SQLite 路径保留 safe fallback：再发一次只取
    `(chunk_id, content_hash, vector)` 的最小标量查询并用
    `_is_finite_vector` 复检，对应 JSON 列缺乏存储端不变量的兜底；
    生产 PostgreSQL 不走这条路径。
  * 函数签名、状态分支、sampled eligibility、retired 跳过、active
    保持 active、空集行为与原实现完全等价；`_is_finite_vector`
    / `_vector_matches` 等纯函数未动。
- 改动 apps/worker/tests/test_embedding_processor.py：
  * 新增 stale hash、missing vector、非有限向量、SQL 编译不含 vector
    payload 四项断言；已有 sampled eligibility / active 保持 / 幂等
    / ready / failed 回归由主任务在隔离镜像复验。
- 风险：SQLite fallback 多发一次候选查询，仅在测试集执行；PostgreSQL
  走纯聚合，不存在 O(N) Python 循环。pgvector `vector_dims` 在向量为
  NULL 时返回 NULL（与存储合同一致）；非有限向量由 pgvector 插入阶段
  直接拒绝，未在本路径暴露。`reconcile_profile_progress` 的调用方
  `apps.worker.services.processor` 未改。
- OpenCode 初稿交付时未跑测试、未部署、未提交；随后由主任务复核并执行下列验证。
  性能代码本地提交 `ae32dc0`，仍按 AGENTS.md 由维护者推进合并与发布。

### 2026-09-17 PDF 碎片诊断与 AI 切片候选（CZ-N04 首批，未启用）

- 分支 `codex/CZ-N04-assisted-chunking`，Codex 主持；OpenCode M3 编写统计聚合
  初稿，Codex 复核后补齐重复任务去重、SQLite 非标准 JSON 防御。ark 调用未产出
  代码，PDF/候选/网页由 Codex 实现。不读取 .env，不部署、不替换生产索引。
- PDF 不再用 `isupper()` 判断标题：标准编号、单位、罗马数字、公式及中英混排
  不再仅因包含大写字母形成独立章节。原文、页码、段落序号不改写。
- 新增纯服务候选构建：AI 只提交连续原文块编号的分组终点；检查顺序、完整覆盖、
  单窗预算及表格邻接，失败退回规则。数据库/Excel 数据集不走逐行 AI 切片。
  对历史 PDF 的错误标题只在候选副本纠正，原结构与当前索引不变。
- 文档详情折叠区主动生成/重新生成候选；管理 POST `/api/documents/{id}/chunking-preview`
  沿用管理员与空间校验，每次最多 2 次模型调用，模型网络超时限制到 10 秒；
  展示规则/候选片段数、短片段数、AI 覆盖块数、失败回退及最多 5 个截断样例。
  未覆盖窗口明确是规则；不宣称全篇 AI 分析。相同来源/模型/策略复用缓存，模型调用
  不持有数据库事务，完成后锁定资料并复核版本/结构指纹，拒绝过期结果。
- 两份实际 PDF 只读规则候选对照：资料 664 子片段 1346→212，短片段 1259→17；
  资料 671 子片段 2586→463，短片段 2369→43。本对比没有模型调用，没有改生产数据，
  **不是检索黄金集，也不能据片段数量下降证明回答质量提升**。
- 隔离 PostgreSQL/pgvector 聚合实测通过重复任务、旧哈希、错维度、等待任务和空集
  5 种情况；测试容器使用内存数据、无外网/宿主端口，完成后已移除，仅删除临时测试数据。
- 全库 AI 切片自动启用仍未交付：CZ-N02 产物版本/原子启用/旧引用保留与 ≥30 个真实
  跨类型黄金问题待完成。ADR-023 明确该门槛；本次不提供危险的直接替换按钮。
- 验证：最终全套 API/Worker 隔离回归 755 项通过（192.23 秒，4 条既有弃用警告）；
  随后补充数据库数据集跳过与标题正文边界保护，候选/缓存/空间/鉴权定向回归
  23 项通过。Web TypeScript 与 ESLint、git diff --check 通过；未做浏览器实测，
  不声称正式环境验收完成。本地提交仅在任务分支，未合并、未推送、未部署。

### 2026-09-18 切片候选与统计性能正式发布

- 维护者明确要求“弄好了合并部署，我要看到效果”，本次据此授权发布；后续任务仍
  遵守 AGENTS.md 的默认部署约束。远端无新增冲突，任务分支快进 main，推送
  `823b9ca`；应用代码 `249a799` / `ae32dc0`，没有移动 v1.0 标签。
- 发布前联合备份 `backups/cangzhi-20260918-005513`，database/storage/metadata
  校验全部通过。API/Web/Worker 旧镜像保留在各自的
  `rollback-before-chunking-20260918` 标签；未删除备份、原文件或旧索引。
- 使用本地基础镜像缓存构建，不改镜像源/代理。新 API `388589569c8c`、Web
  `4c6539e78501`、Worker `abcbd853693b`；镜像内无网络、无生产挂载执行完整
  API/Worker 回归 757 项通过（250.19 秒，4 条既有警告），Web 生产构建通过。
- Chromium 在隔离 SQLite/Next 环境验证桌面 1440×1000、手机 390×844 的候选
  展开、生成、强制重新生成，无页面异常/横向溢出。候选响应使用夹具，验证交互；
  不把它描述为正式浏览器模型调用验收。Chromium 1228 启动异常，换现有 1208 通过。
  临时服务已退出，仅清理隔离测试数据，未使用生产管理员凭据。
- 旧 Worker 收到 TERM 后完成当前批次，于 01:02:30 UTC 正常 exit 0；新 Worker
  01:02:55 UTC 启动，无强杀、无新旧并行消费。两条既有遗留 processing
  `44367` / `44368` 未重置；数据库未重建、未迁移，schema 保持 0031。
- 正式 API/Web 健康端点 200，新候选匿名 POST 401；生产浏览器登录页与受保护
  文档转登录通过。运行 API/Web/Worker 关键源码 SHA-256 与主线相同。
- 正式 API 服务层在环评空间为资料 671 生成候选：版本 672、两次 AI 调用均接受、
  无回退，覆盖 59/11732 个原文块；默认规则子片段 2586→候选 469，短片段
  2369→48，6.5 秒完成。仅保存候选摘要缓存；源文与线上 2586 个子片段不变。
  这不是全篇 AI 切片或检索收益证明，未自动替换历史索引。
- 启动后约 43 秒确认新增完成 29 个向量任务；此前旧 Worker 一批 10 个向量任务
  从 00:54:41 到 01:02:30 才结束。此为短窗口推进验证，不保证固定吞吐，仍有
  大量积压需持续处理。自动 AI 索引启用继续等待 CZ-N02 与黄金集验收。

### 2026-09-18 CZ-R03：恢复碎片化 PDF 检索上下文（未部署）

- 负责人 Codex / OpenCode，分支 `codex/CZ-R03-fragment-context`。M3 提供风险审阅
  及纯函数测试，主负责人完成共享服务、集成测试和真实模型验证；ark 本次调用无代码产出。
- 修改前已实际复现：资料 671 的“GB 14762-2008适用于哪些车辆和发动机？”只召回
  “车速大于 25 km/h”的断句；真实快速问答返回证据不足。候选片段数量下降并未修复
  在线证据完整性，不能把候选预览当作实际效果验收。
- 新增 `fragment_context`：从已授权命中对应的当前文档版本提取同页原始块，识别
  短碎片密集页面并核对原文锚点；保持原序与原字，受块数/字符预算约束。工作空间、
  当前版本、未删除状态再次校验；普通正文、数据集及已识别表格/代码页不做整页拼接。
  不调用模型、不修改原文/切片/向量，不需要新增用户配置或历史全库重处理。
- 搜索沿用现有 `context` 契约与 1800 字符预算；快速问答去除同页重复证据，预算允许
  时避免恢复正文被关键词摘要和单条字符限制二次裁断；深度探索观察/内部读取共用
  恢复逻辑，相同版本页面正文去重，不将不同版本的相同文字合并。
- 公共 `knowledge_get_chunk` 仍返回原片段，未变更授权、公共参数、数据库 schema。
  页面恢复不是跨页完整条款保证；长页面、多栏顺序、文档地图与自动 AI 索引仍待后续。
- 验证：无网络、无生产数据挂载的 API/Worker 回归 781 项通过；最终定向回归
  80 项通过。新增用例覆盖数字/单位/例外、原文不改写、边界预算、普通长段拒绝、
  表格拒绝、同页重复、历史版本/其他空间/回收资料隔离及深度内部读取。
- 用独立 Python 进程和 PostgreSQL 只读事务加载候选代码，读取真实资料与现有模型，
  未替换正在运行的服务。搜索恢复 1031 字符，8 个适用条件/例外检查通过；快速和
  深度回答均完整给出 M2/M3/N2/N3、>3500kg 的 M1、>25km/h 和 M2 等效认证豁免。
  `knowledge_search` 适配器层复测返回相同上下文，未创建凭证、未做真实 HTTP 鉴权验收。
- 深度实际验证期间发现审计 HTTP 400：对现有模型渠道做最小对照，无 JSON 提示
  必现 400，加 JSON 提示成功；补充明确 JSON 输出指令，不改证据审计规则。复测再
  发现终态 next_query=null 被字符串校验拒绝，现将其按未使用字段处理，needs_more
  仍必须有非空检索词。最终真实深度复测完成 1 次证据审计，完整回答且非证据不足，
  两项兼容问题均有测试。真实样本不等于 ≥30 问黄金集，不宣称全库质量已经达标。
- 复核脚本 `python3 scripts/check-fragment-context.py`（快速）、`--deep`（深度）、
  `--search-only`（仅共享搜索/MCP 适配器）；使用固定公开标准回归样本，需本机 API
  已运行且有对应资料，不打印密钥、不读 `.env`。本批未合并、推送或部署。

### 2026-09-18 CZ-R03：按授权部署并验证正式 API

- 维护者本次明确要求“如果没问题就部署验证吧”，据此授权本次发布；不修改
  AGENTS.md 的后续默认约束。直接部署任务分支代码 `9829459`，未合并或推送 main。
- 联合备份 `backups/cangzhi-20260918-072913`，database/storage/metadata 校验通过。
  旧 API 镜像 `388589569c8c` 保留为 `cangzhi-api:rollback-before-fragment-9829459`。
- BuildKit 请求 Docker Hub 基础镜像元数据超时，使用现有 `python:3.11-slim` 与
  依赖缓存，通过经典构建器完成镜像 `cangzhi-api:fragment-9829459`（`3b225793a18f`）；
  没有改镜像源、代理、依赖版本或读取 `.env`。新镜像内无网络、无生产数据挂载的
  API/Worker 完整回归 781 项通过（182.59 秒，4 条既有弃用警告）。
- 07:33:56 UTC 新 API 启动，Compose 等待健康通过。仅重建 API，未重启 Web/Worker/
  PostgreSQL；无迁移，schema 仍是 0031。`make doctor` 全部通过，Web 同源搜索及
  API MCP 匿名调用均为 401，鉴权未绕过。资料 671 当前版本 672、2586 个切片不变。
- 验证脚本增加 `--deployed`：先核对安装服务源码 SHA-256，再直接使用运行镜像代码，
  不注入候选模块。正式数据库只读事务中复测 GB 14762-2008 适用范围：搜索上下文
  1031 字符、8 项条件/例外检查通过；MCP 适配器上下文一致；快速问答和深度分析
  都给出完整车型、速度、质量门槛及 M2 豁免，均非证据不足，深度审计完成 1 次。
  未创建临时凭证、未写入会话记录；不将此描述为带真实令牌的 HTTP/MCP 客户端验收，
  也不将单个真实样本替代跨类型黄金集。
- 回滚仅需将上述旧 API 镜像重新标记为 `cangzhi-api:latest`，再单独重建 API；
  本次无数据库/索引格式变更，不需覆盖恢复数据库。未删除旧镜像、备份或原文。

### 2026-09-18 CZ-N02：先试建，未满足全库重建条件

- 维护者明确要求尝试重建、效果好再全部重建。只读事务中为 6 份真实资料构建内存
  候选；两份 PDF 各最多 2 次 AI 调用，其余类型规则对照，Excel 正确跳过文档分组。
  结果和边界检查见 `docs/RECHUNK_TRIAL.md`，负责人 Codex，任务分支
  `codex/CZ-N02-rechunk-trial`。
- PDF 数量明显下降，但候选命中和同父邻居仍未覆盖一条例外；独立确定性候选
  的命中为 251 字符，现行同页恢复可以补全，未断言候选最终答案退步。线上已部署
  搜索和 MCP 适配器再次验证 8 项条件/例外齐全。本次未构建候选向量或做候选问答。
- 核查 Worker：普通重建不调用 AI 候选；同版本重建删除旧 chunk 后创建新行，
  向量随后异步排队，未实现引用保留、候选就绪后切换与回滚。尚不能安全批量执行。
- 未更改应用代码、生产切片、向量或任务队列；未部署。用户授权中的“效果好”条件
  尚未证实，故未提交全库任务。后续先补 CZ-N02 与跨类型评测，再少量激活和分批扩展。

### 2026-09-18 CZ-N04：显式维护重切与旧流程暂停

- 维护者确认未正式大规模使用，接受旧引用变化和向量补齐空窗，授权备份后首批
  重建验证；随后进一步明确失败和未解析旧流程全部停止、按新流程推进。
- Codex / OpenCode：M3 编写候选参数接入、Worker 显式分支与 7 项用例，ark 编写
  维护选择用例；Codex 审阅修正诊断、异常脱敏及测试夹具，补定向执行器和隔离测试。
  分支 `codex/CZ-N04-assisted-rebuild`，不改公共 API/MCP、默认入库、解析事实源。
- 联合备份 `backups/cangzhi-20260918-082746` 校验通过。旧 Worker 收到 SIGTERM 后
  正常 exit 0；Web/API/PostgreSQL 继续 healthy。暂停时保留 6533 条旧 pending 向量、
  2 条历史 processing；18 条历史解析失败记录未重试，没有待执行解析被批量重置。
- 新镜像 `cangzhi-worker:assisted-trial`（`8617cd966e8b`）无网络、无生产数据挂载下
  API/Worker 810 项通过，4 条既有弃用警告。旧镜像保留为
  `cangzhi-worker:rollback-before-assisted-20260918`；新镜像仅用于一次性定向维护，
  常驻 Worker 继续停止，恢复它前必须处理旧队列。
- 正式首批 7/8/389/664/671 共 5 份完成新策略切片，原文与 structured_content 的
  MD5 均与重建前一致；子片段分别 17/10/23/213/469，前两份 PDF 原为 1346/2586。
  PDF 各最多 2 次模型调用，接受窗口分别 1/2，AI 覆盖分别 0.0054/0.005；其余规则
  处理，不将数量减少解释为全篇 AI 理解。数据集未重切、分类标签未清理。
- 初步检索 5 问仍为 4 问全条件覆盖；法律 Word 的期限词缺口重建前后均存在，
  不归因于本次切片也不宣称已修复。此时新 PDF 向量尚在构建，不能作为最终比较。
  向量仅处理这 5 份新策略任务；普通正文 50 条已完成，PDF 继续。未全库排队，
  尚待当前 profile 完整覆盖与实际问答验收。操作说明见 RECHUNK_MAINTENANCE.md。

### 2026-09-18 CZ-Q05：新上传优先的维护调度（待启用）

- 用户要求继续维护并优先解析新上传。Codex / OpenCode（M3 初稿、ark 审阅）在
  `codex/CZ-Q05-new-upload-priority` 实现独立维护调度入口，复用共享处理服务，
  不改变公共 API/MCP 或普通 Worker 的默认消费行为。
- 新资料以当前文档版本创建时间和显式时区时间界限判断；前台阶段轮转，前台与
  授权历史资料按 4:1 分配任务机会，空池让位。不领取旧解析、旧失败、旧版本、
  回收文档和归档空间；历史名单使用文档 ID，且仅领取新策略切片和向量。
- Codex 补齐空阶段优先权、异常脱敏落库、逐任务停止、会话锁断连退出、超大 PDF
  规则回退等边界。新 PDF 使用受限辅助策略，保留幂等键；不宣称全文 AI 分析。
- 隔离容器（无网络、无生产挂载）运行 API/Worker：850 passed，4 条既有弃用警告，
  181.99 秒；其中 40 项维护调度用例包括真实随手记解析到切片的衔接与旧队列隔离。
- 上一批 7/8/389/664/671 的 732 条当前切片向量已完成并核对内容哈希。
  五问抽查仍 4 问完整覆盖；PDF 条件/例外的实际深度回答验证通过，法律 Word 的
  既有召回缺口仍保留，不据此宣称全库质量已通过。
- 遵守仓库部署边界：未启动新常驻入口，普通 Worker 仍停止，未提交下一批任务，
  未更新 API/Web 服务。维护者启用指引见 RECHUNK_MAINTENANCE.md；新上传后台
  在启用前仍暂停。强制终止恢复、任务租约等 CZ-Q05 剩余项不在本次完成范围。
