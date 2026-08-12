# 藏知（Cangzhi）

> 中文品牌：**藏知** · 英文标识：**Cangzhi**
>
> 产品定位：**个人可控的 AI 知识中枢** · 品牌短句：**藏有所知，问有所据**

藏知把网页、文件、随手记和外部目录持续沉淀为可追溯、可检索、可被人和智能体复用
的长期知识资产。它不只是笔记软件，也不只是“上传文件后聊天”的 RAG 页面；系统
管理从采集、保存、理解、组织、索引、取证到再次利用的完整知识生命周期。

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
- **长期积累需要数据主权**：原文可下载，知识可导出，数据库和文件可备份，模型与
  向量索引可以替换和重建。

完整的问题定义和产品边界见[产品定位](docs/PRODUCT.md)。

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

### 检索与问答

- 确定性识别 general、legal、contract、paper、meeting、code 和 table 文档。
- 根据标题、章节、条款、段落和表格边界生成结构优先的父子切片。
- PostgreSQL 全文检索与 pgvector 向量检索通过 RRF 融合。
- 二维数据集生成可重建的 Parquet 版本，由 DuckDB 下推筛选、投影、排序、分组与
  聚合；API/MCP 只接受受控查询计划，不开放任意 SQL。
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
OCR 深度增强、领域解析和受控写入 API 按[路线图](docs/ROADMAP.md)逐步推进。

## 文档导航

- [产品定位：解决的问题、用户闭环与产品边界](docs/PRODUCT.md)
- [技术架构：事实源、处理管线、检索和安全边界](docs/ARCHITECTURE.md)
- [部署与首次使用：Docker Compose、升级、HTTPS 和排障](docs/DEPLOYMENT.md)
- [开发路线图：已交付基线与下一阶段](docs/ROADMAP.md)
- [当前任务清单](docs/BACKLOG.md)
- [关键技术决策](docs/DECISIONS.md)
- [外部 Agent、CLI 与 MCP 接入](docs/INTEGRATIONS.md)
- [备份与恢复](docs/BACKUP_AND_RESTORE.md)

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

- Web：`GET http://localhost:3000/api/health`
- API liveness：`GET http://localhost:8000/api/liveness`
- API readiness：`GET http://localhost:8000/api/readiness`
