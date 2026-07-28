# 藏知（Cangzhi）

藏知是一个以“低打扰、自动沉淀”为核心的个人知识库。第一阶段优先解决：快速采集、自动解析与分类、混合检索、带原文引用的问答，以及通过用户纠正逐步学习个人偏好。

## 当前状态

M1 已完成：可以创建和编辑随手记，上传 PDF、DOCX、Markdown、TXT，后台自动解析正文输出结构化内容，失败可重试，浏览资料详情，下载原文件。URL 抓取、AI 分类、向量检索和问答仍在开发中。

## 现在可以做什么

- 创建、查看和编辑随手记，每次实质修改保留新版本
- 上传 PDF、DOCX、Markdown、TXT，自动提取正文文本并保持文档结构
- 流式保存原文件并计算 SHA-256，相同内容复用一份 Blob
- 可靠后台异步处理，支持指数退避重试
- 浏览全部资料，详情页显示处理状态和错误信息，处理失败可手动重试
- 下载已保存的原文件和查看解析后的正文

下一步是 URL 入库与 AI 自动分类。混合检索和带引用问答按[开发路线图](docs/ROADMAP.md)逐步加入；邮件转入、浏览器插件、法律/合同深度解析和知识图谱安排在个人 Beta 之后。

## 设计原则

1. **默认自动完成**：高置信度结果直接落库，只有异常资料进入“待整理”。
2. **原文永不丢失**：所有摘要、切片和回答都可以追溯到原文件或网页快照。
3. **结构优先，AI 辅助**：优先依据标题、章节、段落、页码和条款切分，AI 处理结构不清和领域特殊内容。
4. **不是只有向量搜索**：默认采用全文搜索、向量搜索和元数据过滤的混合检索。
5. **配置渐进披露**：普通使用无需写提示词；模型、模板和切片规则放在高级设置中。
6. **可迁移、可重建**：原文和结构化元数据是事实源，索引与向量均可重建。

## 计划文档

- [产品范围与用户流程](docs/PRODUCT.md)
- [技术架构与数据模型](docs/ARCHITECTURE.md)
- [开发路线图](docs/ROADMAP.md)
- [首版任务清单](docs/BACKLOG.md)
- [关键技术决策](docs/DECISIONS.md)

## 推荐首版技术栈

- Web：Next.js + TypeScript
- API：FastAPI + Python
- 数据库：PostgreSQL + pgvector
- 后台任务：数据库任务表 + 独立 Worker；规模扩大后再切 Redis 队列
- 文件存储：本地文件系统；后续兼容 S3/MinIO
- 文档解析：Docling 为主，格式专用解析器兜底
- 检索：PostgreSQL 全文搜索 + pgvector + RRF 融合 + 可选 Reranker
- 部署：Docker Compose，本机或个人服务器单命令启动

## 首次交付定义

当下面这条链路完整可用时，藏知即可开始投入个人使用：

> 粘贴一篇文章链接 → 自动下载和解析 → 自动归类 → 在知识库中找到 → 提问并得到带出处的回答 → 修改错误分类且系统记录纠正。

## 本地开发启动

### 前置要求

- Docker 和 Docker Compose
- Python 3.11+（本地开发时使用）
- Node.js 20+（本地前端开发时使用）

### 快速启动

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 一键启动所有服务：

```bash
make up
```

这会启动：

- PostgreSQL 16 + pgvector
- 自动运行数据库迁移
- FastAPI 应用（http://localhost:8000）
- 后台处理 Worker
- Next.js Web 应用（http://localhost:3000）

打开 Web 后，可以直接选择“记录一个想法”“上传资料”或“查看全部资料”。默认单个文件上限为 50 MB，可在 `.env` 中通过 `MAX_UPLOAD_SIZE_MB` 调整。

### 常用命令

```bash
# 查看服务状态
make ps

# 查看日志
make logs             # 所有服务
make logs-api         # 仅 API

# 重新运行迁移（本地开发）
make migrate

# 运行测试
make test             # 所有测试
make test-api         # 仅 API 测试

# 停止服务
make down

# ⚠️ 停止服务并删除所有数据库数据（清空后无法恢复）
make down-volumes

# 完整安装所有依赖到本地（用于 IDE 提示和本地测试）
make install
```

### 健康检查端点

- Web：`GET http://localhost:3000/api/health`，返回 200 表示正常
- API liveness：`GET http://localhost:8000/api/liveness`，进程存活即返回 200
- API readiness：`GET http://localhost:8000/api/readiness`，数据库连接正常才返回 200
