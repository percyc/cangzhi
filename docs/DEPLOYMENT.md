# 藏知部署与首次使用

本文面向个人服务器、NAS 和局域网主机。默认方案是 Docker Compose：数据库迁移、
API、后台 Worker 和 Web 一次启动，不要求宿主机安装 Python 或 Node.js。

## 1. 部署前准备

必需软件：

- 64 位 Linux；
- Git；
- Docker Engine 24+；
- Docker Compose v2（命令为 `docker compose`）。

小型个人库建议至少 2 核 CPU、4 GB 内存，并为 Docker 镜像、数据库和原文件预留
10 GB 以上空间。大型 Word 转 PDF、Excel/Parquet 构建和本地模型需要更多资源。

检查环境：

```bash
docker --version
docker compose version
git --version
```

国内网络可以在 Docker Engine 层配置已有镜像加速，藏知无需额外代理。Web 镜像内
的 npm 已使用 `npmmirror.com`；Python 和 Debian 软件仍由 Docker 构建阶段联网获取。

如使用数据库知识源，API 容器还需要能直连目标 PostgreSQL/MySQL 主机和端口，不需要
额外代理。私网目标必须在连接器中显式勾选“允许可信内网”。建议在来源库创建只具备
目标 Schema `SELECT` 权限的专用账号，不要使用数据库管理员账号。

## 2. 五分钟部署

```bash
git clone <你的藏知仓库地址> cangzhi
cd cangzhi
cp .env.example .env
```

至少修改 `.env` 中的数据库密码：

```bash
openssl rand -hex 24
```

把输出填入：

```dotenv
POSTGRES_PASSWORD=请替换为足够长的随机密码
```

当前 Compose 会把该值放入数据库连接 URL，因此只使用字母、数字、下划线或连字符，
不要使用 `@`、`:`、`/`、`?`、`#`、`%` 等 URL 保留字符。

首次构建并启动：

```bash
docker compose up -d --build
docker compose ps
```

也可以使用：

```bash
make upgrade
make doctor
```

首次构建会安装 LibreOffice 和中文字体，耗时通常明显长于后续升级。服务依赖顺序为：

```text
PostgreSQL 健康 -> Alembic 迁移完成 -> API 健康 -> Web 与 Worker 启动
```

浏览器打开：

- 本机：<http://localhost:3000>
- 局域网：`http://服务器IP:3000`

首次访问会进入 `/setup`。创建唯一管理员后登录即可使用。管理员密码和数据库密码
是两套不同凭据；日常登录使用前者。

## 3. 首次使用清单

建议按以下顺序完成初始化：

1. 在“设置 → 对话模型”配置 OpenAI-compatible 或 Ollama，并执行连接测试；
2. 在“设置 → 向量与索引”添加 Embedding 配置，先测试，再构建并启用首个索引；
3. 上传一份小 PDF/Word、保存一个网页链接、记录一条随手记；
4. 在“收件箱”确认解析、理解、切片和向量状态；
5. 分别在“搜资料”和“问知识”验证命中与引用原文；
6. 创建第一次系统备份并执行校验。

如需对接外部业务系统，在“设置 → 外部接入”创建绑定目标工作空间的令牌；读取通常选择
`knowledge:read/search`，上传与文档范围同步另选 `documents:write`。Scope Key 不需要
部署配置，关系存储在 PostgreSQL 并随常规备份恢复。

不配置模型也能采集、保存、解析、浏览和全文搜索。对话模型负责自动整理与问答，
Embedding 模型负责向量召回，两者配置和故障互不绑定。

问知识库的历史记录会保存到服务端，便于跨设备回看，但每次提问独立检索，不会
自动读取此前问答。需要长期记忆时，使用 Hermes、OpenClaw 等外部 Agent 通过
MCP/Skill 调用藏知。

## 4. 关键环境变量

日常优先在网页设置模型；环境变量主要控制部署边界。

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `POSTGRES_PASSWORD` | `cangzhi-dev` | 正式部署必须修改；已初始化后修改还需同步数据库凭据 |
| `POSTGRES_BIND_ADDRESS` | `127.0.0.1` | 数据库默认只允许宿主机访问 |
| `POSTGRES_PORT` | `5432` | 宿主机数据库端口 |
| `WEB_BIND_ADDRESS` | `0.0.0.0` | Web 监听地址 |
| `WEB_PORT` | `3000` | Web 和同源 `/api` 入口 |
| `API_BIND_ADDRESS` | `0.0.0.0` | REST/CLI/MCP 直连 API 的监听地址 |
| `API_PORT` | `8000` | API 端口 |
| `STORAGE_HOST_PATH` | `./storage` | 原文、预览、Parquet 和本地主密钥的宿主机目录 |
| `MAX_UPLOAD_SIZE_MB` | `50` | 单文件上传上限 |
| `URL_FETCH_MAX_BYTES` | `5242880` | 网页抓取最大响应字节数 |
| `DATASET_QUERY_MEMORY_LIMIT` | `512MB` | 单次 DuckDB 查询内存边界 |
| `DATASET_QUERY_THREADS` | `2` | 单次 DuckDB 查询线程数 |
| `DATASET_QUERY_TIMEOUT_SECONDS` | `20` | 数据集执行超时 |
| `CANGZHI_SECRET_KEY` | 空 | 可选 Fernet 主密钥；为空时写入 `storage/.secret_key` |
| `CANGZHI_WEB_BASE_PATH` | 空 | 可选 Web 子路径；详见下方“子路径网关” |
| `CORS_ALLOWED_ORIGINS` | 空 | 仅跨域直接调用 API 时配置，逗号分隔完整 Origin |

Compose 内部数据库地址由 `POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB` 生成，
`.env` 中的 `DATABASE_URL` 只供宿主机本地开发使用。

### 4.1 加密主密钥

默认让系统在首次启动时生成 `storage/.secret_key` 最省事。该文件必须与数据库一起
备份，否则已保存的模型 Key 和 WebDAV 密钥无法解密。

如果使用外部密钥管理，可在首次启动前生成 Fernet 兼容密钥：

```bash
openssl rand -base64 32 | tr '/+' '_-'
```

把输出写入 `.env`：

```dotenv
CANGZHI_SECRET_KEY=生成的完整值
```

API 和 Worker 必须始终使用同一个值。系统已有加密数据后不可随意轮换或丢失该密钥。

如需把原文件放到独立磁盘，可在首次启动前设置：

```dotenv
STORAGE_HOST_PATH=/mnt/data/cangzhi/storage
```

目录必须同时对 Docker、API 和 Worker 可读写。已有数据时不能只修改路径，必须先在
服务停止状态下完整迁移原 `storage/` 内容并核对 `.secret_key`。

### 4.2 监听地址建议

- 只在本机使用：`WEB_BIND_ADDRESS=127.0.0.1`，`API_BIND_ADDRESS=127.0.0.1`；
- 局域网使用：Web 设为 `0.0.0.0`，API 若需 CLI/MCP 直连也设为 `0.0.0.0`；
- 公网使用：Web/API 都可只监听 `127.0.0.1`，由反向代理统一提供 HTTPS；
- PostgreSQL 保持 `127.0.0.1`，不要直接暴露公网。

外部平台也可以直接使用 Web 的同源入口 `https://你的域名/api/mcp` 和
`https://你的域名/api/v1`，因此公网通常不必单独开放 8000 端口。

### 4.3 子路径网关（可选）

默认情况下 Web 部署在 `http://<host>:<WEB_PORT>/` 根路径。当需要把藏知挂到外部反向
代理的子路径（例如 `https://example.com/_cangzhi/`）时，设置：

```dotenv
CANGZHI_WEB_BASE_PATH=/_cangzhi
```

注意：

- 该变量在 Compose 中既作为 Docker 构建参数 `ARG` 传给 `apps/web/Dockerfile`，
  也作为容器运行时 `ENV` 注入 Next.js。Next.js 会把它固化为 `basePath` 与
  `NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH`，浏览器侧的 `<Link>`、资源引用、
  `withBasePath` / `withApiBasePath` 都依赖这个值。
- 因此**该值在镜像构建时固化**。修改后必须重新构建 Web 镜像：
  ```bash
  docker compose build web
  docker compose up -d web
  ```
- Web 容器的 `healthcheck` 与 `make doctor` 都会按当前 `CANGZHI_WEB_BASE_PATH`
  拼出正确的健康地址（默认根路径：`/api/health`；设了子路径：`/_cangzhi/api/health`）。
- 不配置或留空时 Web 始终是根路径部署，健康地址直接是 `/api/health`。

## 5. 公网 HTTPS

公网部署必须使用 TLS。下面是最小 Nginx 示例，假设藏知 Web 只监听本机 3000：

```nginx
server {
    listen 443 ssl http2;
    server_name knowledge.example.com;

    ssl_certificate     /etc/letsencrypt/live/knowledge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/knowledge.example.com/privkey.pem;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 深度分析和 MCP 使用流式响应
        proxy_buffering off;
        proxy_read_timeout 180s;
    }
}
```

对应 `.env`：

```dotenv
WEB_BIND_ADDRESS=127.0.0.1
API_BIND_ADDRESS=127.0.0.1
```

反向代理、防火墙或安全组只开放 80/443。不要把 `storage/`、`.env`、PostgreSQL 或
Docker Socket 暴露给 Web。

## 6. 局域网与 WebDAV

从局域网访问时，使用服务器的固定 IP 或内网域名。WebDAV 连接由 Worker 发起：

- 公网 WebDAV 默认按 URL 安全规则访问；
- 私有 IP/NAS 地址需要在连接器中显式选择“信任私有网络”；
- Worker 容器必须能够解析域名并连通目标端口；
- WebDAV 始终只读，不会删除或改写远端文件。

如连接失败，先在服务器验证 DNS 和 TLS，再查看：

```bash
docker compose logs --tail=200 worker
```

## 7. 日常运维

### 7.1 状态与日志

```bash
make ps
make doctor
make logs-api
make logs-worker
make logs-web
```

健康端点：

- Web：`GET http://127.0.0.1:3000/api/health`（默认根路径；自定义 `CANGZHI_WEB_BASE_PATH` 时把 `/_cangzhi` 替换为该子路径，例如 `http://127.0.0.1:3000/_cangzhi/api/health`）
- API 存活：`GET http://127.0.0.1:8000/api/liveness`
- API 就绪：`GET http://127.0.0.1:8000/api/readiness`

`make doctor` 还会显示当前数据库迁移版本。`migrate` 容器正常退出码为 0，不需要
长期保持 running。

### 7.2 停止与启动

```bash
docker compose stop          # 保留容器和数据
docker compose start
docker compose down          # 删除容器和网络，保留数据库卷与 storage/
```

不要运行 `docker compose down -v`，除非明确要永久删除数据库卷。`storage/` 是宿主机
目录，不会随普通 `down` 删除，但仍需要独立备份。

## 8. 升级

升级前先备份：

```bash
make backup
git pull --ff-only
make upgrade
make doctor
```

`make upgrade` 会重新构建镜像、运行 `alembic upgrade head`，然后只重建发生变化的
服务。数据库迁移成功前 API 不会启动。

不要在新 Schema 已写入数据后直接切回旧代码，也不要手工降低
`alembic_version`。需要回退时，优先恢复升级前的数据库与 `storage/` 联合备份。

若基础镜像元数据偶发超时，但服务器已经缓存全部基础镜像，可临时使用经典构建器：

```bash
DOCKER_BUILDKIT=0 docker compose build
docker compose up -d
```

这只是复用本地缓存的故障恢复方式，不需要给容器额外配置代理。

## 9. 备份与恢复

创建和校验：

```bash
make backup
make verify-backup BACKUP=backups/cangzhi-YYYYMMDD-HHMMSS
scripts/rehearse-restore.sh backups/cangzhi-YYYYMMDD-HHMMSS
```

备份同时包含 PostgreSQL、自托管原文件、预览、Parquet 和加密主密钥。知识库页面的
ZIP 导出适合阅读和迁移，不等同于可恢复整个系统的运维备份。

正式恢复步骤见[备份与恢复](BACKUP_AND_RESTORE.md)。

## 10. 常见故障

### 页面打不开

```bash
docker compose ps
docker compose logs --tail=200 migrate api web
```

若 `migrate` 失败，先解决数据库或迁移错误，不要绕过它强行启动 API。

### 登录后反复返回登录页

- 确认浏览器访问的协议和域名没有来回切换；
- 反向代理必须传递 `Host` 和 `X-Forwarded-Proto`；
- 检查 API 与数据库时间是否正确；
- 不要同时用多个不同域名混用同一页面。

### 文档一直处理中

```bash
docker compose logs --tail=300 worker
```

在“收件箱”查看具体阶段。解析、理解、切片、数据集、预览和向量是不同任务；没有
配置模型时，理解/向量可能降级，但原文不应丢失。

排障时区分“等待领取”和“正在执行”：容器健康只代表进程可用，不代表队列没有
积压。检查最近完成时间与各阶段等待数量，不要把旧切片或旧向量的 completed
计数当作本次重处理已完成。历史重处理应按本次解析任务及后续产物核对。

Worker 调度改动需要维护者更新 Worker 后才会生效；仅拉取源码不会改变运行中的
容器。发布时让旧 Worker 正常结束当前工作，避免新旧消费者同时处理同一文档。
对于长期停留 processing 的遗留任务，先确认原进程已经退出，再按恢复流程处理；
不要批量把 processing 重置为 created，以免正在执行的任务被重复运行。

### 模型测试成功但问答不可用

- 确认测试的是“对话模型”而不是仅配置了 Embedding；
- 检查实际启用的模型名和 Base URL；
- OpenAI-compatible Base URL 通常应包含 `/v1`；
- 查看 API 日志中的脱敏错误，不要把 Key 发到聊天或 Issue。

### 磁盘增长过快

```bash
du -sh storage
docker system df
docker exec cangzhi-postgres psql -U cangzhi -d cangzhi -c \
  "SELECT pg_size_pretty(pg_database_size('cangzhi'));"
```

原文、PDF 预览、Parquet、历史版本和多套向量索引都会占用空间。先从页面删除不用的
知识或旧索引并清空回收站，不要直接修改数据库或删除 `storage/` 文件。

## 11. 部署验收

完成部署或升级后，至少确认：

- `make doctor` 全部通过；
- `alembic_version` 是当前代码的 head；
- 能登录并上传、下载一份原文件；
- Worker 能把示例资料处理到可检索状态；
- 搜索结果能打开原文定位；
- 问答能显示引用，模型不可用时错误清晰；
- 备份校验和隔离恢复演练通过；
- 公网环境只能访问预期的 HTTPS 端口。
