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
