# 藏知（Cangzhi）

[简体中文](README.md) | [English](README.en.md)

> 中文品牌：**藏知** · 英文标识：**Cangzhi**
>
> 产品定位：**个人可控的 AI 知识中枢** · 品牌短句：**藏有所知，问有所据**

藏知把网页、文件、随手记和外部目录持续沉淀为可追溯、可检索、可被人和智能体复用
的长期知识资产。它不只是笔记软件，也不只是“上传文件后聊天”的 RAG 页面；系统
管理从采集、保存、理解、组织、索引、取证到再次利用的完整知识生命周期。

## AI 驱动体现在哪里

藏知所说的“AI 驱动”，不是在传统知识库外面增加一个聊天框，而是让 AI 同时参与
知识的使用和沉淀：

- **为外部 AI 提供自主探索路径**：藏知通过 REST API、CLI、MCP 和 Skill，把知识
  发现、检索、上下文扩展、文档阅读、数据集筛选聚合和证据取证拆成可组合的工具。
  外部 AI 保留自己的推理、规划与工具编排能力，可以根据中间结果继续追问、改变
  检索策略和交叉验证证据，而不是只能接收平台一次检索或一次回答的结果，也不受
  藏知内置模型能力上限约束。藏知负责提供可靠的知识探索路径和可核验事实，最终
  如何思考与回答由接入它的 AI 决定。
- **AI 原生的知识整理**：内容入库后，AI 结合文档类型、正文结构和已有分类体系参与
  摘要、归类与标签决策，并为不同类型资料选择合适的解析和切片方式。默认流程尽量
  自动完成，用户可在需要时纠正结果，而不必为每篇资料预先编写规则。
- **可核验、可控制、可重建**：AI 的回答必须回到文档版本、原文片段或数据原始行；
  模型、提示词、分类结果和向量索引均可调整或重建。AI 提升理解与决策能力，但不
  取代原文事实源和用户的数据控制权。

## 为什么需要藏知

- **保存不该以整理为前提**：内容先可靠入库，AI 在后台完成摘要、分类和标签，异常
  集中进入收件箱。
- **文件不等于知识**：原文和版本之外，系统保留章节、条款、页码与段落结构，再生成
  可重建的切片和索引。
- **检索不该只依赖向量**：关键词、向量和元数据过滤双路召回并融合；向量服务异常时
  自动降级。
- **回答必须能核对**：知识问答引用具体文档版本和原文片段，证据不足时不会用模型
  常识冒充知识库内容。
- **知识不该锁在一个应用里**：网页、REST API、CLI、MCP 和 Skill 共享相同的知识
  范围、检索与引用逻辑。
- **第三方选择可以成为可信探索边界**：普通 PAT 仍支持工作空间级 MCP；需要把某次
  文档选择交给外部 AI 时，可签发短期、不可扩大的知识探索凭证，供 Skill 与 MCP
  共用，而无需引入复杂的用户角色权限系统。
- **长期积累需要数据主权**：原文可下载，知识可导出，数据库和文件可备份，模型与
  向量索引可以替换和重建。

完整的问题定义和产品边界见[产品定位](docs/PRODUCT.md)。

## 版本与当前开发重点

- **`v1.0` 是保留的已验证基线**，对应提交 `8cccfb9`；Gitea、Gitee 的同名标签一致。
  后续开发不移动或覆盖该标签。本 README 描述当前开发主线，不是 v1.0 的功能清单；
  使用旧基线时应阅读该标签下的文档。
- **`main` 是持续调试中的开发主线**，尚未发布新的正式版本。当前实现已包含可选知识
  增强、原文导航、检索证据补充及 Excel 解析修复，不代表全部质量目标已验收。
- **下一项重点是非规则 Excel 的结构化探索**：保留标准二维表的 Parquet/DuckDB
  精确查询，增加单元格事实、逻辑区域与按需阅读，再逐步引入受校验的 AI 结构识别。
  不能把整张复杂表拆成向量后，就宣称理解了表头、层级或统计口径。

接手开发请先读 [AGENTS.md](AGENTS.md)、[项目状态](docs/PROJECT_STATUS.md)，再读
[开发交接](docs/DEVELOPER_HANDOFF.md)及[测试资料说明](docs/SPREADSHEET_TESTING.md)。

## 当前可用能力

### 采集与保存

- 创建和编辑 Markdown 随手记，实质修改保留版本。
- 上传 PDF、DOC、DOCX、XLSX、XLS、Markdown 和 TXT，并下载保存的原文件。
- 收藏公开网页，保存 HTML 快照并提取标题、作者、时间和正文。
- 连接 WebDAV 目录，只读扫描并沿用统一解析、分类和索引流程。
- 连接 PostgreSQL 或 MySQL，只读浏览 Schema/表并导入可重复更新的本地数据快照。
- 原文件按 SHA-256 去重存储；后台任务失败可见、可重试。

### 理解与组织

- 内置十个一级分类，也可增加、改名和删除未使用分类。
- 可选 OpenAI-compatible 或 Ollama 对话模型，自动生成摘要、分类和标签。
- 未配置模型或模型不可用时继续完成基础入库，资料进入待整理。
- 保存可用模型后自动补跑历史未整理资料；用户可手动覆盖主分类。
- 统一管理知识回收、恢复、永久删除以及 WebDAV 外部来源生命周期。
- 可选知识增强第二批代码已实现：空间开关、章节/实体关系分析、原文证据、预算进度、
  取消与续跑。基础 AI 整理不变，不替换检索索引；需同版服务及迁移 0032 后使用，
  发布状态和边界见[使用说明](docs/KNOWLEDGE_ENHANCEMENT.md)。
- 已构建增强产物支持 REST / MCP / CLI / Skill 按窗口和视图只读探索，附原文证据与
  覆盖进度；读取不调用模型，外部 AI 保持自主分析，局部解释不等于全文理解。
- 可另外启用分层概览：在同一调用预算内逐层汇总，支持从概览下钻到窗口原文。
  需要迁移0033；已有配置不自动升级，同名实体只提供待核对线索，不自动合并。
- 不开启增强也可通过 REST / MCP / CLI 分页浏览原文目录和块，读取版本绑定的
  精确原文；外部 AI 自行选择探索路径，不额外调用藏知模型。

### 检索与问答

- 确定性识别 general、legal、contract、paper、meeting、code 和 table 文档。
- 根据标题、章节、条款、段落和表格边界生成结构优先的父子切片。
- 文档详情提供 **AI 辅助切片候选评估**：有限窗口内让模型建议原文块边界，程序
  校验完整性并对比片段数量与样例。候选尚不替换现有索引；不是全篇自动 AI 切片。
- PostgreSQL 全文检索与 pgvector 向量检索通过 RRF 融合。
- 二维数据集生成可重建的 Parquet 版本，由 DuckDB 下推筛选、投影、排序、分组与
  聚合；API/MCP 只接受受控查询计划，不开放任意 SQL。
- XLSX 解析按实际值/公式范围排除纯样式空白，保留资源保护。非规则区域目前保守
  回退为带行列位置的全文检索；AI 区域识别、层级条目与单元格范围探索仍属待开发能力。
- 分类、标签、来源类型和知识范围同时约束关键词与向量候选。
- 搜索结果提供命中片段、章节路径和原文定位。
- 问知识库支持选择全部知识、随手记、网页、文件或保存的知识范围，并返回可点击
  引用；问答记录同步到服务端供跨设备回看，但每次提问独立检索，不自动读取历史问答。

### 模型、数据与外部接入

- 对话模型和 Embedding 渠道独立配置、独立测试和快速切换。
- 向量索引版本支持后台全库构建、失败重试、原子启用和一键回滚。
- 单篇知识可导出 Markdown/JSON，全库可导出可脱离藏知阅读的 ZIP。
- 提供 PostgreSQL 与 storage 联合备份、校验和隔离恢复演练。
- `/api/v1`、`python -m apps.cli` 和 `/api/mcp` 可供 Hermes、OpenClaw 等外部
  Agent 取证。
- 每个客户端使用独立、最小权限、可撤销的个人访问令牌。

## 核心工作方式

```text
链接 / 文件 / 随手记 / WebDAV / 数据库表
              ↓
      原文与版本可靠保存
              ↓
   解析结构 → AI 理解与组织
              ↓
 全文索引 + 版本化向量索引
              ↓
 浏览 / 搜索 / 引用问答 / Agent 取证
```

AI 是增强能力，不是可靠保存的前置条件；原文和结构化元数据是事实源，摘要、切片、
向量等派生数据都可以重建。

## 当前定位与边界

当前版本服务单个用户和个人服务器，目标是成为个人知识与 AI 工具之间的中枢，而
不是通用网盘、双向文件同步工具或团队协作平台。多用户权限、邮件采集、浏览器扩展、
OCR 深度增强和领域解析按[路线图](docs/ROADMAP.md)逐步推进。文件上传与 Scope Key
范围管理已提供受控 API，但不扩展为通用文档管理或多用户权限平台。

## 文档导航

- [开发交接：项目目标、后续三批工作与交付提示词](docs/DEVELOPER_HANDOFF.md)
- [Excel 测试资料：自动测试、合成样例与私有原件交接](docs/SPREADSHEET_TESTING.md)
- [协作者约定：开工前必读与协作约束](AGENTS.md)
- [项目状态：当前阶段、进行中、风险与下一步](docs/PROJECT_STATUS.md)
- [开发日志：按时间追加的变更记录](docs/DEVLOG.md)
- [产品定位：解决的问题、用户闭环与产品边界](docs/PRODUCT.md)
- [技术架构：事实源、处理管线、检索和安全边界](docs/ARCHITECTURE.md)
- [部署与首次使用：Docker Compose、升级、HTTPS 和排障](docs/DEPLOYMENT.md)
- [开发路线图：已交付基线与下一阶段](docs/ROADMAP.md)
- [当前任务清单](docs/BACKLOG.md)
- [关键技术决策](docs/DECISIONS.md)
- [外部 Agent、CLI 与 MCP 接入](docs/INTEGRATIONS.md)
- [API / MCP / CLI 接口使用参考](docs/API_REFERENCE.md)
- [备份与恢复](docs/BACKUP_AND_RESTORE.md)
- [外部系统对接：API 上传与文档 Scope Key](docs/EXTERNAL_CLIENT_ACCESS.md)

## 技术栈

- Web：Next.js + TypeScript
- API：FastAPI + Python
- 数据库：PostgreSQL + pgvector
- 后台任务：数据库任务表 + 独立 Worker
- 文件存储：本地文件系统，可演进到 S3/MinIO
- 文档解析：格式专用解析器与 LibreOffice 转换
- 检索：PostgreSQL 全文搜索 + pgvector + RRF
- 部署：Docker Compose

## 快速部署

### 前置要求

- Docker Engine 与 Docker Compose v2
- Git

使用 Compose 部署不要求宿主机安装 Python 或 Node.js。

### 快速启动

```bash
cp .env.example .env
# 正式使用前修改 .env 中的 POSTGRES_PASSWORD
docker compose up -d --build
make doctor
```

服务包括：

- Web：<http://localhost:3000>
- API：<http://localhost:8000>
- PostgreSQL 16 + pgvector
- 后台处理 Worker
- 自动数据库迁移

首次打开 Web 会进入 `/setup` 创建唯一管理员，之后从 `/login` 登录。默认单文件
上限为 50 MB，网页快照上限为 5 MB。

模型是可选配置。登录后在“设置”中分别配置对话模型和 Embedding 模型；网页配置
立即生效并覆盖 `.env` 中的兼容配置，密钥加密保存在服务器，页面不会回显明文。

完整的局域网/公网部署、端口、HTTPS、国内网络、升级、备份和排障说明见
[部署与首次使用](docs/DEPLOYMENT.md)。

### 常用命令

```bash
make ps               # 查看服务状态
make doctor           # 检查容器、迁移版本和健康端点
make upgrade          # 构建新代码、自动迁移并更新服务
make logs             # 查看所有服务日志
make logs-api         # 仅查看 API 日志
make backup           # 同时备份 PostgreSQL 与 storage
make migrate          # 运行数据库迁移
make test             # 运行所有测试
make test-api         # 仅运行 API 测试
make down             # 停止服务
make install          # 安装本地开发依赖
```

`make down-volumes` 会同时删除数据库数据，只能在明确需要清空环境时使用。

### 健康检查

- Web：`GET http://localhost:3000/api/health`（默认根路径部署；配置 `CANGZHI_WEB_BASE_PATH=/_cangzhi` 后为 `GET http://localhost:3000/_cangzhi/api/health`）
- API liveness：`GET http://localhost:8000/api/liveness`
- API readiness：`GET http://localhost:8000/api/readiness`

### 子路径网关（可选）

默认情况下 Web 部署在 `http://localhost:3000/` 根路径；如果要把它挂到外部网关的
子路径（例如 `https://example.com/_cangzhi/`），在 `.env` 中设置：

```dotenv
CANGZHI_WEB_BASE_PATH=/_cangzhi
```

该变量在 `compose.yaml` 中既会作为 Docker 构建参数 `ARG` 传给 `apps/web/Dockerfile`，
也会作为容器运行时 `ENV` 注入 Next.js。值会被 Next.js 固化为静态资源与 `basePath`，
**修改后必须重新构建 Web 镜像**：

```bash
docker compose build web
docker compose up -d web
```

不修改 `.env` 时 Web 始终是根路径部署，健康地址直接是 `/api/health`。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源，允许修改、分发及商业使用，但须保留原始
版权声明和许可证文本。第三方依赖、模型及导入资料仍分别遵循其自身许可。
