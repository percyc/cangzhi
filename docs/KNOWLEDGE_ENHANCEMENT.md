# 可选 AI 知识增强：管理与运行

本页对应 ADR-024 第二至五批实现，需要迁移 `0033` 以及同版本 API、Worker、Web。
代码交付不等于已部署；当前发布状态以 PROJECT_STATUS 为准。

## 基础整理不变

正文提取、OCR、基础 AI 摘要/分类/标签、规则切片、全文与向量检索继续原有流程。
知识增强是独立补充层，不是这些能力的总开关。本批增加窗口内章节理解、实体、关系、
事件和对应原文证据，不替换正文、当前切片或向量，也尚未参与问答召回。

增强结果是模型解释，证据编号有效不等于内容语义已经验证。实体 ID 只在分析窗口内
有效；本批没有把同名人物跨章节自动合并，没有生成全书统一关系图。

## 使用方式

1. 切换到目标工作空间，进入「设置 → 知识增强」。默认关闭。
2. 选择章节理解、实体关系与事件；可另外勾选依赖章节理解的分层概览（overview），
   既有设置不自动勾选。设置每文档初始调用预算（1–32 次，默认 8）。
   确认额外模型费用及原文内容外发给已配置的对话模型后保存。
3. 仅生效时间以后的新版本在正文解析成功后自动入队。历史资料不会自动全库重跑，
   可以在文档详情的「知识增强」折叠面板确认费用后手动开始。
4. 面板显示完成窗口/总窗口、已处理原文字数、已用/获批调用次数、失败信息，
   分页展开窗口摘要与原文证据。没有提取到信息与尚未分析是不同状态。
5. 预算不足或失败可以再次确认费用后追加 1–32 次继续，总计最多 256 次。
   成功窗口不重复处理，失败窗口按明确的继续操作重新尝试。
6. 取消保留已完成结果。正在进行的外部请求可能无法立刻中断，已经外发的内容和费用
   不能撤回，但迟到结果不会发布。关闭空间开关只停止未来自动入队，不代替取消。

新版本/结构变化后旧结果不能作为当前结果继续发布；需要为当前版本重新开始。
执行租约为 180 秒，异常中断后可在租约到期后人工继续，不隐式无上限重试。
设置、列表与结果读取都不会调用模型。模型不可用时展示失败，基础入库不受影响。

## 预算和规模

- 初始及每次追加预算按模型调用**尝试次数**计数，不是 token 额度或价格承诺。
  发送前持久化计数；无效输出、超时以及发送后进程中断也消费一次预算。
- 单窗口最多 6,000 原文字符、32 段；超长块保留精确字符区间及局部标记。
  可选 overview 在局部窗口全部完成后按最多四个子结果逐层归纳，共享调用预算；
  树按连续窗口而非真实章节分组。面板分别展示窗口覆盖与概览完成度。
  单次归纳最多输入8,000摘要字符，不重复发送整篇正文；预算耗尽可追加继续。
  概览带子结果引用，下钻至窗口原文核验；完成不代表语义已验证或所有细节均被摘要。
- 当前每次运行限制 20,000 原文块、2,000,000 字符、4,096 窗口。
  超限明确拒绝，不悄悄抽样后标为全文完成。未覆盖部分会显示在进度中。
- Excel/数据库数据集保持目录与精确数据查询，不逐行调用模型。
- 运行固定文档版本、结构指纹及模块配置；相同源与配置的重复开始返回已有运行。
  模型身份记录在每个窗口上，续跑可以使用调整后的模型，不伪称所有窗口同源模型。

## 管理接口

以下接口仅面向已登录管理员，沿用当前工作空间，不接受 PAT 作为管理授权。
它们不是 `/api/v1` 的公共写契约。第三批新增公共只读探索，见下节；外部凭证仍不能
开始、续跑、取消或修改设置。

| 方法与路径 | 用途 |
|---|---|
| GET /api/enhancement/settings | 当前空间配置与已支持模块 |
| PUT /api/enhancement/settings | enabled、modules、call_budget、cost_acknowledged；生效时间由服务端生成 |
| GET /api/documents/{id}/enhancements | 最近 20 次运行摘要及 current_version_id |
| POST /api/documents/{id}/enhancements | 确认 cost_acknowledged 后为当前版本开始 |
| GET /api/enhancements/{id}?offset=0&limit=5 | 窗口分页（limit 1–10）、进度、原文区间与解释结果 |
| GET /api/enhancements/{id}/overview?node_key=L1:0 | 只读分层概览；省略 node_key 返回根节点 |
| POST /api/enhancements/{id}/cancel | 取消，不删除已有结果 |
| POST /api/enhancements/{id}/resume | additional_calls（1–32）与 cost_acknowledged |

已回收文档、其他空间以及归档空间的运行不允许读取或继续；通用工作空间 settings
更新不能绕过费用确认、生效时间校验修改知识增强配置。

## 维护与后续

公共只读探索复用同一运行和证据：REST `/api/v1/knowledge/documents/{id}/enhancements`
及 `/api/v1/knowledge/enhancements/{run_id}`，MCP `knowledge_list_enhancements` /
`knowledge_get_enhancement`，CLI `enhancements` / `enhancement`。一个请求选择一个
窗口及一种视图，按需读取摘要、实体、关系、事件或原文证据。要求 `knowledge:read`，
沿用文档选择与临时凭证边界，过期源不可作为当前知识读取。能力发现及完整参数见
[API参考](API_REFERENCE.md#39-已构建的知识增强产物)。读取不调用模型，也不切换索引。

迁移只新增 `knowledge_enhancement_runs/windows/nodes`，不提交历史任务，不改已有索引。
普通 Worker 识别 `knowledge_enhancement`；优先维护执行器也接受截止时间之后显式
创建的增强任务，不因此恢复旧解析队列。发布仍由维护者执行，先备份与验证迁移。
回退到旧程序可以保留新增表；降级迁移会删除增强派生产物，应由维护者明确决定。

新增只读 `knowledge_get_enhancement_overview`、REST 同名 overview 路径和 CLI
enhancement-overview，提供相同下钻路径。直接子窗口同名/别名仅作为实体候选线索，
保留窗口和局部实体编号，绝不自动合并。旧运行不会自动增加概览任务；改变模块后
需明确重新开始（相同源与配置仍幂等），不是继续旧运行就升级配置。

第五批原文探索不依赖增强开关：`knowledge_get_document_map` 分页读取解析器标题
或全部原序块，`knowledge_get_document_block` 按版本/指纹绑定的块 ID 核对原文。
REST 和 CLI 同步支持，无模型调用。没有标题不等于没有正文；超大结构降级到既有
分页原文或数据集工具；表格块字符分页不等于完整行或结构化单元格查询。

后续交付仍包括语义章节级汇总、实体消歧、表格单元格结构探索、候选切片版本、
原子启用/回滚、旧引用保留和跨类型真实黄金集。不能以测试通过或切片变少宣称检索
质量已经改善。

## 验证入口

- API/Worker：新增 `test_enhancement_analysis`、`test_knowledge_enhancement`、
  `test_enhancement_api` 测试文件，常规套件默认使用隔离 SQLite。
- PostgreSQL 并发专项：`test_enhancement_postgres.py` 仅在显式设置
  `CANGZHI_TEST_POSTGRES_URL` 且数据库名为 `cangzhi_enhancement_test` 时执行；
  应使用无生产网络/数据卷的临时数据库，先迁移到 0033。
- 浏览器：`scripts/check-enhancement-ui.mjs` 针对独立 Next.js 测试页面，全部 API
  响应模拟。根页面嵌入 `<KnowledgeEnhancement docId="1" />`，设置页使用实际组件；
  设置 `ENHANCEMENT_TEST_URL`（默认本机 3107）、`PLAYWRIGHT_MODULE`，可选已有
  浏览器路径 `PLAYWRIGHT_CHROMIUM_PATH`。覆盖 1280px/375px，不能替代真实模型验收。
