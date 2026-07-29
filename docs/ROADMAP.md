# 藏知开发路线图

时间估算按一名开发者配合 AI 工具计算，是排序依据而不是对外承诺。每个里程碑结束都必须能独立演示和使用。

## M0：工程骨架（1～2 个开发日）

目标：项目可以一条命令启动，具备持续开发基础。

- 初始化 Git、README、许可证选择和代码规范
- 创建 Next.js、FastAPI、Worker 三个应用
- Docker Compose 启动 PostgreSQL + pgvector
- 建立 Alembic 迁移和基础数据模型
- 建立 `.env.example`，支持 OpenAI-compatible 与 Ollama 配置
- 健康检查、结构化日志、基础测试和 CI

验收：新环境按照 README 在 10 分钟内启动 Web、API、Worker 和数据库。

## M1：可以开始存资料（3～5 个开发日）

目标：用户能真正把个人资料放进藏知，并可靠保存。

- 随手记创建与编辑
- 文件上传、SHA-256 去重、原文件保存
- URL 抓取、正文抽取、网页快照
- 文档列表、详情和处理状态
- Docling/格式解析器接入
- 统一结构树和基础预览
- 后台任务状态机、失败重试
- 默认分类树

验收：连续导入 20 份真实资料，原文不丢失，处理失败可见且可重试。

## M2：自动整理（3～4 个开发日）

目标：资料进入后基本不需要手工整理。

- 文档类型识别
- AI 标题清理、摘要、分类和标签
- Processing Profile 与 Prompt 版本
- 分类置信度及“待整理”策略
- 分类/标签修改与纠正记录
- 相似文件和重复内容提示

验收：用至少 50 份个人资料测试，80% 以上不需要重新选择一级分类。

## M3：搜索与问答（4～6 个开发日）

目标：存进去的资料可以准确找回和引用。

- ✅ 结构优先父子切片（M3-1）
- ✅ PostgreSQL 全文索引（M3-1）
- ✅ 单轮带引用问答（M3-2）：基于父子切片的 FTS 证据召回 + OpenAI/Ollama 结构化回答
- ✅ 单用户认证与网页模型设置（M3-3）
- ✅ 确定性文档类型检测与类型适配切片（M3-4）
- ✅ 模型列表查询与快速选择（M3-5）
- ✅ Embedding 模型配置与连接验证（M3-6a）
- **当前依然使用关键词全文召回**，向量召回和 RRF 融合后续处理
- pgvector Embedding 与 HNSW 索引
- BM25/FTS + 向量 + 元数据过滤
- RRF 融合和可选 Reranker
- 搜索结果片段高亮（M3-1 提供元数据，后续完善）
- 多轮对话记忆
- 点击引用定位章节、页码或段落（M3-1 已记录定位字段）
- 首批 20～30 个检索黄金问题

M3-1（已交付）：解析成功后由 Worker 写入 `document_chunks`（父/子），
包含 `document_version`、`parent_id`、序号、类型、正文、heading_path、
`page` / `paragraph_index` / `source_start` / `source_end`、`content_hash`，
幂等可重建；`/api/search` 在 PostgreSQL 上使用 `tsvector` + GIN 索引，
SQLite 测试环境安全降级到 `LIKE`；新增 `/search` 页面、空/无结果/错误
状态友好、结果可点回资料详情。

M3-3（已交付）：单用户认证与网页模型设置。

* Alembic 迁移 ``0005`` 新增 ``admins`` / ``auth_sessions`` /
  ``ai_runtime_configs`` 三张表；管理员表和 AI 配置表都通过
  ``singleton_key`` 唯一约束在数据库层强制单行，所以即使应用层
  检查被绕过，并发 ``setup`` 或重复提交也只会留下第一行。
* 单用户首次启动流程：未设置账户时 ``/setup`` 创建唯一管理员
  （scrypt 哈希，盐随机），之后 ``/login`` 登录、``/logout`` 退出、``GET
  /api/auth/status`` 查询状态。
* 会话使用 32 字节 URL-safe 随机 token，HttpOnly + SameSite=Lax
  Cookie，12 小时 TTL；数据库只存 token 的 SHA-256，泄露数据库
  不会让 token 可重放。``/login`` 按 IP+账户限流，``/setup`` 按
  IP 限流，分别为每分钟 5/3 次。
* 除健康检查、``/api/auth/*`` 外所有 ``/api`` 路由都挂
  ``Depends(require_admin)`` 依赖；测试可通过
  ``app.dependency_overrides[require_admin]`` 注入 stub，没有任何
  绕过生产可用的开关。``CANGZHI_AUTH_DISABLED`` 之类的环境变量
  已被删除。
* 新增 ``/settings``：选择禁用 / OpenAI 兼容 / Ollama，配置 base
  URL、模型与 API key。key 在数据库中以 Fernet 密文保存（密钥从
  ``storage/.secret_key`` 或 ``CANGZHI_SECRET_KEY`` 环境变量读
  取），页面只返回 ``has_api_key`` 布尔值。连接测试用最小探测
  调用并对响应做脱敏，禁止回显密钥或上游原始错误。
* AI provider 加载新增 ``build_provider_from_db``（异步，API 路径）
  和 ``build_provider_from_session``（同步，Worker 路径），并
  保留 ``build_provider`` 作为环境变量回退：Web 配置始终覆盖
  ``.env``，Worker 在每个任务里重新读取数据库配置。
* Next.js ``proxy.ts``（原 ``middleware.ts``）只做浏览器引导，
  API 才是最终安全边界；统一顶部导航包含资料库、搜索、问知识
  库、设置、退出；``/ask`` 在未配置模型时提示并直接链接到
  ``/settings``。

M3-2（已交付，首版）：在 M3-1 的 FTS 之上交付单轮带引用问答。
`apps/api/services/qa.py` 完成证据召回（中文自然问题通过归一化关键词
和 2-gram/3-gram 召回，避免要求整句命中），`AIProvider.answer_question`
为 OpenAI-compatible 与 Ollama 提供统一的结构化 JSON 输出；
`AnswerResult` 严格校验：充分回答必须引用至少一条证据，引用 id 只能
来自提供的证据，证据不足时必须返回 `insufficient_evidence: true`。
新增 `POST /api/ask` 与 `GET /api/ask/status`，未配置模型返回 503、
模型失败返回 502 且不泄露 API Key、问题长度和证据数量受限。
新增非技术中文 `/ask` 页面：问题输入、加载态、未配置模型、无证据、
失败态、答案、引用卡片；首页和资料列表加入“问知识库”入口。
检索黄金集与“80% Top 5 命中”指标仍按 M3 验收执行，本版本明确
**不是基于向量检索的混合问答**，向量召回、RRF、Reranker 仍按
ADR-006 与 ADR-011 排到 M3 后续。

验收：黄金集中，正确证据进入 Top 5 的比例达到 80%；回答不能伪造引用。

M3-4（已交付，本次迭代）：确定性文档类型检测与类型适配切片。

* 新建 `apps/api/documents/` 模块：检测依据解析文档类型、块类型占比、标题/正文关键词，保守评分后输出文档类型，低置信度强制降级为 general。
* 支持输出类型：`general/legal/contract/paper/meeting/code/table`。
* 类型感知分块：`general` 保持当前行为；`code/table` 使用更大块参数尽量保持单个代码块/表格块完整；其他类型首版识别后记录元数据，分块暂时使用 general 参数；特殊规则足够可靠后再添加专属参数。
* 清理未生效的父片段尺寸参数：父片段明确保存完整章节，子片段按类型配置控制尺寸，避免配置与真实行为不一致。
* 元数据写入：`DocumentVersion.meta` 写入 document_profile、chunking_config、structure_anomaly；每个 `DocumentChunk.extra` 写入类型信息；检测分块幂等可重复处理，显式 reprocess 后获得新策略，不批量重处理已有文档。
* 模型连接测试改进：OpenAI-compatible /models 响应必须验证 JSON 结构，并且检查配置模型是否存在于返回列表；HTML 2xx 不再判定成功；错误信息严格脱敏。
* **提示词版本收口**：prompt_version 不再允许网页设置和 API 用户修改，仅作为内部提示词包版本追溯，保留在 DocumentSummary 作为处理记录；Worker 从 DB 加载实际运行配置获取版本，不再使用环境默认值；首版仅支持 v1，未知版本安全回退。
* 兼容 SQLite 和 PostgreSQL 两种数据库，代码不绑定特定后端。

验收：检测结果确定，相同文档重复处理得到相同元数据；大代码文档切片保持块完整性；模型连接测试正确识别 HTML 响应为失败；连接测试不泄露敏感信息。

M3-5（本次迭代，已交付）：模型列表查询与快速切换。

* 新增已鉴权 `GET /api/settings/ai/models`：从配置好的服务拉取可用模型列表，OpenAI-compatible 调用 `/models`，Ollama 调用 `/api/tags`；统一返回 `{models: [{id, label?}], provider, current_model}`。
* 安全规则：仅使用数据库已保存配置，不允许请求参数传入任意 URL/key；上游错误脱敏，不泄露密钥和上游响应正文；非 JSON/结构异常/HTTP 错误返回明确安全的错误提示；模型按 id 去重自然排序，最多返回 500 条。
* `/settings` 页增加“获取模型列表”按钮；查询成功后通过原生 datalist 提供快速选择，始终允许手动输入；当前模型不在列表中仍保留；provider/地址/密钥变化自动清空旧列表。首次配置可以先只保存地址和密钥，再获取模型并二次保存。
* 复用已有 probe 的模型解析逻辑，不重复实现；前端移除 `AIConfigPayload` 遗留的 `prompt_version`，保持类型定义干净。

验收：拉取成功后可快速选择已拉模型，配置变更自动失效旧列表；错误提示友好，不泄露敏感信息；排序去重符合预期，手动输入始终可用。

M3-6a（本次迭代，已交付）：Embedding 模型配置与连接验证。

* `/settings` 可选 Embedding 模型，复用现有服务地址、密钥和模型列表；留空即关闭，不影响关键词检索。
* OpenAI-compatible 使用 `/embeddings`，Ollama 使用 `/api/embed` 做短文本连接测试，并校验返回向量非空、数值有效。
* 连接错误和上游响应均做脱敏处理；本轮尚未生成或检索文档向量。

验收：配置可保存、清除和重新读取；两类服务均可单独测试；未配置时原有问答和全文检索保持不变。

M3-6b（已交付）：可切换的版本化向量索引。

* 将 Embedding 渠道从聊天配置中拆分，支持独立 provider、地址、密钥、模型和超时。
* 新增候选配置测试：返回连接状态、维度、canary 指纹，以及相对当前索引的
  `same / compatible / rebuild_required / unknown` 判定和中文原因。
* 新增 `embedding_profiles`、`chunk_embeddings` 和向量构建任务；旧索引继续服务，
  新索引覆盖全部当前 child chunks 后才能原子激活。
* 支持构建进度、失败重试、取消、激活和回滚；同内容同 profile 幂等，不重复计费。
* 新文档和内容更新自动为 active/building profile 生成向量。

当前进度：独立渠道、固定 canary 兼容性测试、版本档案、后台全库构建、
失败重试、进度展示、原子切换与回滚均已交付。新切片会自动为 active/building
档案补充向量；混合检索在 M3-6c 接入。

验收：更换密钥不要求重建；更换同名渠道时 canary 能识别兼容与漂移；更换维度
必须重建；构建失败时旧索引继续可用；激活与回滚均不产生混合空间。

M3-6c（已交付）：混合检索与问答接入。

* 查询只使用 active profile 生成 query embedding。
* FTS/BM25 与向量召回分别取候选，通过 RRF 融合；元数据过滤同时作用于两路。
* Embedding 服务不可用时自动降级为全文检索，并在响应中标明降级原因。
* 用黄金问题集比较 FTS、向量和混合检索的 Top 5 命中率，再决定是否增加 Reranker。

验收：任何结果都能追溯到原文切片；混合检索不低于单路最佳结果；向量故障不阻断搜索。

当前进度：搜索与问答已共用 active profile 查询向量，使用 RRF 融合关键词和
向量候选；分类、标签、来源过滤同时作用于两路。响应与页面均展示实际召回模式
和降级原因。生产库已验证向量能够召回没有字面命中的资料。黄金问题集和
Reranker 对比作为后续质量调优任务持续维护，不阻塞首版混合检索使用。

## M4：个人 Beta 稳定性（3～5 个开发日）

目标：可以长期保存个人资料，而不只是演示。

- 回收站和恢复
- Markdown/JSON 导出
- 数据库及 storage 备份脚本和恢复文档
- API Key 安全存储
- URL 抓取 SSRF 防护
- 文件限制和 HTML 清洗
- 模型费用、任务耗时和失败率统计
- 大文档、扫描 PDF 和异常网络测试

验收：完成一次全量备份、清空测试环境后恢复，并重新生成全部索引。

## M5：快速采集增强（个人 Beta 后）

- 专用邮箱或 IMAP 轮询
- 浏览器扩展
- 手机分享入口或 PWA 快速记录
- 批量导入现有文件夹
- RSS/稍后读导入

## M6：领域能力（后续）

- 法律：编章节条款项、效力、适用范围、义务、例外和引用关系
- 合同：主体、金额、期限、义务、违约和风险
- 论文：问题、方法、数据、结论、局限和参考文献
- 会议：决策、待办、责任人和截止时间
- 智能专题、关系发现及选择性 GraphRAG

## 里程碑控制规则

- M1 前不做复杂知识图谱。
- M2 前不做提示词可视化编辑器，只保留配置文件和版本表。
- M3 的检索评测未达标，不通过增加更大回答模型掩盖召回问题。
- M4 完成前不把系统暴露到公网。
- 每个新数据源必须先满足“原文保存、幂等、失败可重试”再接入 AI。
