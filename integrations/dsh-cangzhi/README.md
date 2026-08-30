# 藏知 DSH 插件

这是基于 DeepSeek Harness（DSH）的藏知原生插件。它不是 iframe，也不要求浏览器直接访问藏知的 localhost 端口。

新版提供：

- DSH 首页原生藏知登录与连接引导；
- 侧栏“藏知 DSH”品牌和对话输入框知识状态条；
- 上传、知识搜索、文档管理、重新处理、网页收录、随手记和分类管理；
- 14 个可由 DSH 大模型直接调用的藏知 MCP 工具；
- PAT 自动创建、验证和安全存储；
- DSH 同源 API 网关和仅监听回环地址的 MCP 凭据代理。

## 本机目录

当前机器使用以下目录：

```text
藏知项目：/data/share/cangzhi
DSH 源码：/home/percy/software/deepseek-harness
插件目录：/data/share/cangzhi/integrations/dsh-cangzhi
DSH Profile：/home/percy/.dsh/profiles/web
```

## 前置服务

新版 DSH 原生页面只依赖藏知 API 和 worker。藏知 Next.js Web 不是核心功能的必需项。

先在藏知项目目录启动生产服务：

```bash
cd /data/share/cangzhi
docker compose up -d postgres api worker
```

检查 API：

```bash
curl http://127.0.0.1:8000/api/readiness
```

正常结果应包含：

```json
{"status":"ok","database":"ok"}
```

## 首次安装或代码更新后

DSH 基座增加了 `conversation.hero.extension` 和 `conversation.hero.headline` 两个通用首页槽位。因此首次安装、拉取新代码或者修改插件后，需要重新构建基座扩展并安装插件。

推荐执行：

```bash
export DSH_SOURCE=/home/percy/software/deepseek-harness
cd /data/share/cangzhi
./integrations/dsh-cangzhi/scripts/setup-dsh.sh
```

脚本会依次完成：

1. 构建 DSH conversation UI 基座；
2. 对新增首页槽位执行 TypeScript 检查；
3. 构建藏知插件 Host 与 Client；
4. 安装或更新 `web` Profile 中的 `dsh-cangzhi`。

## 日常启动

确认 3080、3081 端口没有旧 DSH 进程，然后执行：

```bash
export DSH_SOURCE=/home/percy/software/deepseek-harness
cd /data/share/cangzhi
./integrations/dsh-cangzhi/scripts/start-dsh.sh
```

默认配置：

```text
DSH 页面：http://127.0.0.1:3080
藏知 API：http://127.0.0.1:8000
内部 MCP 凭据代理：http://127.0.0.1:3081
DSH Profile：web
```

启动成功后，终端会打印带一次性启动 token 的地址，例如：

```text
dsh web: http://127.0.0.1:3080/?token=...
```

必须打开本次启动打印的新地址。旧启动地址中的 token 在 DSH 重启后不能继续使用。

可选环境变量：

```bash
export DSH_HOST=127.0.0.1
export DSH_PORT=3080
export DSH_PROFILE=web
export CANGZHI_API_URL=http://127.0.0.1:8000
```

不要为日常启动设置 `CANGZHI_MCP_URL`，否则会绕过插件的安全凭据代理。

## 首次登录与 MCP 认证

打开新版 DSH 后，新会话首页会直接显示：

- 藏知用户名；
- 密码；
- “登录并连接”按钮。

点击“登录并连接”后会自动完成：

1. 登录藏知；
2. 创建只包含 `knowledge:read`、`knowledge:search`、`knowledge:ask` 的 DSH 专用 PAT；
3. 使用藏知 API 验证 PAT；
4. 将 PAT 写入 DSH 凭据存储；
5. 让 MCP 客户端自动重连；
6. 注册 14 个 `mcp__cangzhi__knowledge_*` 工具。

不需要手工填写 `CANGZHI_TOKEN`，页面也不会回显或读取已经保存的密钥。

如果首页显示“尚未认证”，说明还没有完成上述登录流程。进入“藏知管理中心 → 对话接入”也可以查看、断开或重新建立连接。

## 停止和重启

前台启动时，在启动 DSH 的终端按 `Ctrl+C` 即可停止。之后重新运行：

```bash
./integrations/dsh-cangzhi/scripts/start-dsh.sh
```

脚本不会自动终止已有进程。如果提示 3080 或 3081 已占用，请先在旧 DSH 终端按 `Ctrl+C`，避免误杀其他服务。

## 手工构建与安装

不使用脚本时，可执行：

```bash
cd /home/percy/software/deepseek-harness
pnpm --filter @deepseek-ai/dsh-client-ui-conversation run bundle
pnpm exec tsc -b packages/client/ui-conversation/tsconfig.json --pretty false

cd /data/share/cangzhi/integrations/dsh-cangzhi
DSH_SOURCE=/home/percy/software/deepseek-harness \
  /home/percy/software/deepseek-harness/node_modules/.bin/tsdown \
  --config tsdown.config.ts
node scripts/rewrite-client-id.mjs

cd /home/percy/software/deepseek-harness
node apps/cli/lib/bin.js plugin --profile web remove dsh-cangzhi || true
node apps/cli/lib/bin.js plugin --profile web add \
  file:/data/share/cangzhi/integrations/dsh-cangzhi

env -u CANGZHI_MCP_URL \
  CANGZHI_API_URL=http://127.0.0.1:8000 \
  node apps/cli/lib/bin.js --profile web \
  --host 127.0.0.1 --port 3080 --no-open
```

## 常见问题

### 页面仍是旧版

重新执行 `setup-dsh.sh`，重启 DSH，然后打开终端新打印的 token 地址。必要时强制刷新浏览器页面。

### MCP 提示未认证

先确认首页已经使用藏知管理员账号执行“登录并连接”。然后检查：

```bash
curl http://127.0.0.1:8000/api/readiness
```

内部 3081 端口返回 503 且提示 `CANGZHI_TOKEN is not configured`，表示登录连接尚未完成；完成后 MCP 会自动重连。

### DSH 返回 401

直接访问 `http://127.0.0.1:3080/` 返回 401 是正常安全行为。请使用启动终端输出的 `?token=...` 地址交换浏览器登录 Cookie。

### 端口占用

```bash
ss -ltnp | rg ':3080|:3081|:8000'
```

- 8000：藏知 API；
- 3080：DSH Web；
- 3081：插件内部 MCP 代理。

## SQLite 开发模式

生产环境建议使用 PostgreSQL/pgvector。本地演示也可以使用 SQLite：

```bash
cd /data/share/cangzhi
export DATABASE_URL=sqlite:////data/share/cangzhi/storage/cangzhi.db
.venv/bin/python integrations/dsh-cangzhi/scripts/init-dev-sqlite.py
.venv/bin/uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```
