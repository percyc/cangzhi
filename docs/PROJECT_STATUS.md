# 藏知项目状态

> 当前快照。`ROADMAP.md` 是阶段与交付节点，`BACKLOG.md` 是未完成任务，
> `DEVLOG.md` 是按时间追加的变更记录，三者互不重复。本文件只回答：
> **现在在哪、正在做、刚完成、有什么风险、下一步是什么。**

更新时间：2026-09-15

## 1. 当前阶段

个人 Beta 质量闭环（见 `ROADMAP.md` §2）。已验证部署基线保存在 `v1.0` 标签，
指向 `8cccfb9`，标签已推送 origin；后续文档理解增强在独立任务分支推进。

代码已交付至路线图基线 A–G（含个人多工作空间、结构化数据库来源、独立数据集
Parquet/DuckDB 执行后端、文档 Scope Key 与短期知识探索凭证），后续阶段目标
是把真实个人资料长期运行后仍然可靠、准确、容易维护，**不再继续堆入口**。

## 2. 已交付基线（摘要）

完整说明见 `ROADMAP.md` §1，本节只列锚点便于新协作者快速对位：

- **A 可靠沉淀**：随手记 / 文件 / 网页采集，原文 + 哈希 + 版本。
- **B 自动组织**：分类树、AI 摘要与标签、无模型降级与历史补跑。
- **C 检索与问答**：父子切片、FTS + pgvector + RRF、版本化向量档案、原子启用
  与回滚、深度分析 Plan–Act–Observe。
- **D 生命周期与数据主权**：回收站、WebDAV 只读、导出、PostgreSQL + storage
  联合备份与隔离恢复演练。
- **E 知识中枢**：统一 `KnowledgeScope`、REST v1、CLI、MCP Streamable HTTP、
  `integrations/skills/cangzhi-knowledge/` Skill 模板。
- **F 个人多工作空间**：单管理员可创建 / 切换 / 归档工作空间；文档、分类、
  标签、知识范围、WebDAV、问答记录按空间隔离；模型、备份仍全局共享。
- **G 结构化数据库来源**：PostgreSQL/MySQL 加密连接、只读浏览、本地数据集
  快照、DuckDB 受控查询。

## 3. 进行中任务

按 `BACKLOG.md` 优先级排列；状态字段在每次推进时由维护者更新。

| 主题 | 关联 ID | 状态 | 备注 |
|---|---|---|---|
| 文档理解演进第一批 | `CZ-Q06` | 发布前完善 | 负责人 Codex / OpenCode；分支 `codex/CZ-Q06-document-structure`；原序修复 674 项已通过，继续补标题跳级回归；维护者授权验证后合并部署，不自动重建历史索引 |
| 外部视觉 OCR（基线 H 第二阶段） | `CZ-D01b` | 收尾 | 解析、Provider、触发条件、外发页数限制、`metadata.pdf_extraction` 与 `Block.extra` 扩展已交付；剩下黄金集评测与处理中心可视化 |
| 处理状态与可观察性 | `CZ-Q03` / `CZ-Q04` | 进行中 | 实时全局队列统计、处理中心阶段口径和当前 profile 向量覆盖已交付；剩下 DSH 自动刷新验收、耗时、失败类型、模型调用与大文档回归集 |
| 检索质量评测 | `CZ-R01`–`CZ-R05` | 进行中 | Reranker 暂不引入；黄金集与分路 Top-K 评测待补 |
| 外部来源稳定性 | `CZ-S01`–`CZ-S04` | 进行中 | 来源诊断页面已有雏形；定时扫描、抓取恢复策略待完成 |
| 问答记录增强 | `CZ-U02` | 进行中 | 服务端保存与删除已交付；重命名 / 归档 / 搜索 / 按原范围重跑待补 |

## 4. 最近完成（时间倒序）

完整记录见 `DEVLOG.md`；本节只列最近若干条以便快速对位。

- 2026-09-14 全页面验收与发布完成（CZ-Q08）：运行代码 `0519319` 已合并、
  推送 origin/main 并部署。52 个隔离桌面/手机页面与状态检查通过；生产镜像内
  API/Worker 656 项通过。手机收件箱改为卡片；API、Web、Worker、PostgreSQL
  均 healthy，schema `0031`。环评空间新服务只读复测 402 份资料，首 25 条约
  2.128 秒/21 KB。完整记录见 `PAGE_REVIEW.md`；两条发布前遗留向量任务待 CZ-Q05。

- 2026-09-14 收件箱性能修复已验证，按维护者要求合并本地 main：提交 `f177867`，
  来自分支 `codex/CZ-Q07-inbox-performance`，
  负责人 Codex / OpenCode；精简状态查询、服务端分页筛选、刷新串行与取消。
  API/Worker 656 项通过，Web lint/typecheck 通过；隔离 Chromium 验证分页、
  本页批量操作、旧响应丢弃、慢轮询、隐藏页暂停及超时重试。真实环评空间只读
  试跑全部页约 2.3 秒、失败筛选约 2.0 秒；已随 `0519319` 发布。

- 2026-09-14 解析可靠性第一批完成验证：派生文本 Unicode 规范化、元数据字段
  冲突显式失败、Word PDF 预览保存点隔离与异常脱敏、OCR 空结果状态修复。
  负责人 Codex / OpenCode；修复提交 `1413786`，来自
  `codex/CZ-Q04-parser-reliability`；完整 API 与 Worker 测试 653 项通过。
  按维护者要求合并至 main，已随 `0519319` 发布；未手动重处理历史资料。

- 2026-09-01 全局处理队列改按当前、未删除资料的实时任务聚合，向量子任务按资料
  去重，失败版本优先于残留任务；DSH 自动刷新仍待独立插件补齐。
- 2026-08-31 数据集受控查询响应补齐 `document_version_id`、标题与规范化
  `query_plan`，外部 DSH 可按产物版本和贡献原始行构建精确证据入口。
- 2026-08-31 外部视觉 OCR 第二阶段实现：解析可插拔 Provider、触发条件
  短路、外发页数上限、本地降级与不写密钥／图片字节到日志。
- 2026-08-30 处理中心与文档详情阶段口径统一，向量覆盖按当前 profile 与切片
  内容哈希计算。
- 2026-08-19 文档 Scope Key 重命名与短期知识探索凭证（`cz_eg_...`），Skill
  REST 与 MCP 全部只读工具与服务端授权边界强制取交集。
- 2026-08-19 单管理员多工作空间交付，REST / CLI / MCP / Skill 可显式选择空间。
- 2026-08-13 受控写入 API：`/api/upload` 稳定 v1、`external_id` 幂等版本更新、
  `documents:write` 最小权限、文档 Scope Key 单篇与批量管理。
- 2026-08-04 数据集 Parquet/DuckDB 执行后端：单数据集筛选聚合下推、资源边界、
  原子版本切换；数据集计划与语义路由可重建。

## 5. 风险与未决项

1. **黄金评测未建立。** RRF 融合与切片边界只能凭单例子判断，没有 ≥30 个真实
   问题支撑。所有“检索优化”类改动须先建评测再上代码，否则会反复推翻。
2. **数据库连接器仍按 10 万行上限。** 大表的增量同步、CA 证书校验、筛选 /
   列选择导入、后台进度与取消均未交付，导入大表前必须先在来源库建立只读视图。
3. **不可写 OpenAPI 自动审计。** `/api/v1` 仍为只读契约，新增写入端点必须
   配套速率限制、写入审计与 `Idempotency-Key`。
4. **OCR 第二阶段评测缺位。** 本地 vs 外部多模态 vs 二者组合的 Top-K 命中率与
   失败模式尚未量化，外发页数阈值与触发条件仍是经验值。
5. **Agent 多协作者刚建立基线。** `AGENTS.md`、状态页和开发日志已经落地，
   但还需要在实际并行任务中坚持“一个任务一个分支、先登记再开发”的约定，
   避免状态页和日志出现竞争修改。

## 6. 验证

- 代码：`make test`（API `pytest` + Worker `pytest` + Web `npm run lint` &
  `npm run typecheck`）。
- 部署：`make doctor` 检查容器状态、Alembic head 与 API `/api/readiness`、
  Web `/api/health`。
- 备份：`make backup` 后 `make verify-backup BACKUP=backups/cangzhi-...` 校验
  校验和与元数据。
- 检索：黄金集脚本（待落地，路径 `tests/retrieval/golden/`）输出分路 Top-K
  命中率与失败模式。
- 恢复：`scripts/rehearse-restore.sh` 在隔离环境演练 PostgreSQL + storage
  联合恢复，不覆盖生产数据。

## 7. 下一步

按优先级整理（不在此重复 `BACKLOG.md` 的完成定义）：

- DOCX 原序修复已在任务分支验证，等待合并；后续文档地图、派生产物版本与
  分层 AI 理解按 DOCUMENT_UNDERSTANDING_PLAN.md 推进。Worker 心跳/超时恢复
  仍待完成；历史资料定向重试需维护者明确发起。

1. 完成 `CZ-D01b` 评测闭环：本地 vs 外部 vs 组合的 Top-K 命中率、失败模式
   落表，并在处理中心与文档详情页可视化。
2. 建立 `CZ-R01`–`CZ-R02` 黄金集与分路评测脚本，作为后续检索改动的准入门槛。
3. 推进 `CZ-S01`–`CZ-S04` 来源稳定性：WebDAV 定时扫描、批量限流与断点续扫、
   网页抓取失败分类与重试建议。
4. 补齐 `CZ-U02` 问答记录：重命名、归档、搜索、按原范围重跑。
5. 写“受控写入 API”后续：URL / 随手记稳定 v1、`Idempotency-Key`、写入审计、
   速率限制与大小限制（先 ADR 后代码）。

## 8. 索引速查

- 路线图：[ROADMAP.md](./ROADMAP.md)
- 当前任务：[BACKLOG.md](./BACKLOG.md)
- 变更日志：[DEVLOG.md](./DEVLOG.md)
- 技术架构：[ARCHITECTURE.md](./ARCHITECTURE.md)
- 关键技术决策：[DECISIONS.md](./DECISIONS.md)
- 协作者约定：[../AGENTS.md](../AGENTS.md)
