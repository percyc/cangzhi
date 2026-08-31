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
