# AGENTS.md

> 给人类协作者与多智能体协作者共同遵守的开工约定。读完这一份，才能动手改代码。

## 1. 这份文件解决什么

藏知目前是单人维护的代码库，但会有多个人类协作者和多个 Agent（包括自动化 Agent
与外部 Skill）同时在上面工作。每个人／每个 Agent 启动时都需要相同的上下文：
项目现在做到哪、接下来要做什么、哪些事不要做、有哪些文档要先读。

`AGENTS.md` 是开工的入口，`docs/PROJECT_STATUS.md` 是当前快照，`docs/DEVLOG.md`
是按时间追加的变更记录。三者各司其职，不重复 `ARCHITECTURE.md` /
`ROADMAP.md` / `BACKLOG.md` / `DECISIONS.md` 的内容。

## 2. 开工前必读（按顺序）

每次接手新任务之前，按下列顺序阅读 1–5 项；第 6 项按需：

1. `README.md` —— 了解产品定位、当前能力、部署入口与文档导航。
2. `docs/PROJECT_STATUS.md` —— 了解当前阶段、正在做、刚完成、风险与下一步。
3. `docs/ROADMAP.md` —— 了解已交付基线和下一阶段目标，避免重做。
4. `docs/BACKLOG.md` —— 了解尚未完成的具体任务与完成定义。
5. `docs/ARCHITECTURE.md` —— 了解模块边界、生命周期、检索、问答与安全约束。
6. 相关 ADR：
   - 大方向：先看 `docs/DECISIONS.md` 的目录，定位和当前任务相关的 ADR。
   - 外部 Agent / Scope Key：阅读 `docs/EXTERNAL_CLIENT_ACCESS.md` 与
     `docs/ADR-021-external-client-access.md`、`docs/ADR-022-mcp-exploration-grants.md`。
   - 外部知识库接入：阅读 `docs/ADR-017-knowledge-hub-adapters.md`。
7. 动手之前的最近变更：`docs/DEVLOG.md` 末尾 3–5 条；旧的变更无需通读。

读完之后，再决定是否还需要回到 `docs/PRODUCT.md` / `docs/DEPLOYMENT.md` /
`docs/INTEGRATIONS.md` / `docs/API_REFERENCE.md` / `docs/BACKUP_AND_RESTORE.md`
查阅特定主题。

## 3. 协作约束（所有协作者共同遵守）

1. **不读取 `.env`，不记录任何密钥。** 文档、对话、日志、PR 描述、Commit
   message 都不得包含 `CANGZHI_SECRET_KEY`、`POSTGRES_PASSWORD`、
   `OPENAI_API_KEY`、PAT 明文、文档 Scope Key 明文或探索凭证明文。`.env`
   由部署者本地维护，本仓库不追踪。
2. **事实源不能被破坏。** 原文件、网页快照、随手记原文、`document_versions`
   是事实源。摘要、分类、切片、向量、Parquet、PDF 预览都是可重建派生物。新代码
   不能让解析或模型失败导致原文丢失，也不能在未构建完成前替换当前服务索引。
3. **HTTP 层只做校验、鉴权、DTO 转换。** 检索、范围解析、问答、引用、数据集
   执行必须放在服务层，网页、REST、CLI、MCP、Skill 必须复用同一逻辑，不能各自
   再实现一遍。
4. **不可逆操作必须由用户明确发起。** 后台任务只能执行可恢复动作。
5. **不擅自修改公共契约。** `/api/v1` 稳定只读、`/api/upload` 文件上传、
   `documents:write` 权限与文档 Scope Key 行为变更需要先写 ADR 并在
   `docs/DEVLOG.md` 留痕，再动代码。
6. **不在本仓库做部署。** 不要执行 `docker compose up`、`make up`、
   `make upgrade`、推送镜像、触发 CI 部署等动作。代码、迁移、测试、文档交付
   即可，部署由维护者负责。
7. **不在本仓库做 Git 高风险操作。** 未经明确允许，不 force-push、不
   rebase 已推送分支、不删除远程分支、不重写历史。提交粒度以单一意图为限。
8. **新代码必须配测试与文档。** 完成定义参考 `docs/BACKLOG.md` 的表格列与
   `docs/ROADMAP.md` 的验收段落；解析、切片、检索融合、数据集计划、权限、
   URL 安全尤其必须有单元或集成测试。

## 4. 工作流建议

下面是一条建议路径，可以根据具体任务调整，但不要跳步：

1. 读 `PROJECT_STATUS.md` 的“当前阶段”、“进行中任务”、“风险”和“下一步”。
2. 读 `BACKLOG.md` 中相关 ID（例如 `CZ-R01`），明确完成定义。
3. 在 `apps/` 下定位模块：API 业务在 `apps/api/`、后台任务在 `apps/worker/`、
   界面在 `apps/web/`、CLI 在 `apps/cli/`、外部 Skill 模板在
   `integrations/skills/cangzhi-knowledge/`。
4. 本地命令参考 `Makefile`：迁移 `make migrate`、API 测试
   `make test-api`、Worker 测试 `make test-worker`、前端校验
   `make test-web`。
5. 完成后：
   - 把变更追加到 `docs/DEVLOG.md`（追加式，不重写旧条目）。
   - 必要时更新 `docs/PROJECT_STATUS.md` 的“进行中 / 最近完成 / 风险 / 下一步”。
   - 不要把开发日志混进 `ROADMAP.md` / `BACKLOG.md` / `DECISIONS.md`：
     - 阶段与交付节点写 `ROADMAP.md`。
     - 未完成任务写 `BACKLOG.md`。
     - 取舍写 ADR，登记在 `DECISIONS.md`。

## 4.1 多人并行时的最小约定

- 一个任务对应一个分支和一个负责人；分支名建议包含任务 ID，例如
  `codex/CZ-R01-retrieval-eval`。
- 开始编码前先在 `PROJECT_STATUS.md` 的“进行中任务”登记负责人、分支和目标；
  不要同时占用别人的同一行，发现重叠先协调再改代码。
- `DEVLOG.md` 只能追加，禁止为了整理格式改写历史；多人同时追加时以合并冲突
  最小的独立段落提交。
- 合并前由负责人把状态页的进行中任务移动到最近完成或风险，并附上提交号和
  测试结果；未完成内容继续保留在 `BACKLOG.md`。

## 5. 角色边界（人和 Agent 都适用）

- **人类维护者**：拥有最终决策权，负责 ADR 审批、合并、部署、密钥管理。
- **人类协作者**：在 Git 上提交 PR，先在 `PROJECT_STATUS.md` 与 `BACKLOG.md`
  登记意图。
- **代码 Agent**：可以读写代码、迁移、测试、文档，但需要遵守第 3 节所有约束。
- **外部接入 Agent**（Hermes、OpenClaw 等）：只消费 `/api/v1`、`/api/mcp` 与
  `integrations/skills/cangzhi-knowledge/`，不直接读写本仓库。

## 6. 索引速查

- 文档导航：[README.md](./README.md)
- 当前状态：[docs/PROJECT_STATUS.md](./docs/PROJECT_STATUS.md)
- 变更日志：[docs/DEVLOG.md](./docs/DEVLOG.md)
- 产品定位：[docs/PRODUCT.md](./docs/PRODUCT.md)
- 技术架构：[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- 路线图：[docs/ROADMAP.md](./docs/ROADMAP.md)
- 当前任务：[docs/BACKLOG.md](./docs/BACKLOG.md)
- 关键技术决策：[docs/DECISIONS.md](./docs/DECISIONS.md)
- 部署与首次使用：[docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md)
- 外部 Agent / CLI / MCP 接入：[docs/INTEGRATIONS.md](./docs/INTEGRATIONS.md)
- 外部系统文档范围：[docs/EXTERNAL_CLIENT_ACCESS.md](./docs/EXTERNAL_CLIENT_ACCESS.md)
- 备份与恢复：[docs/BACKUP_AND_RESTORE.md](./docs/BACKUP_AND_RESTORE.md)
- API / MCP / CLI 参考：[docs/API_REFERENCE.md](./docs/API_REFERENCE.md)
