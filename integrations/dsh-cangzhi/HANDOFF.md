# 藏知 DSH 插件开发交接

更新日期：2026-08-30  
当前插件版本：0.8.0  
藏知项目：`/data/share/cangzhi`  
DSH 源码：`/home/percy/software/deepseek-harness`  
插件目录：`/data/share/cangzhi/integrations/dsh-cangzhi`  
DSH Profile：`/home/percy/.dsh/profiles/web`

## 1. 产品目标

项目名称是“藏知”。目标不是在 DSH 中嵌入藏知网页，而是把 DeepSeek Harness 作为藏知的主要使用入口：

- DSH 大模型对话能够通过 MCP 主动搜索、读取、问答和引用藏知内容；
- DSH 原生页面能够登录藏知，并管理上传、文档、分类、知识空间和处理任务；
- 对话页面是高频全功能工作台，聊天、资料检索、原文预览和当前对话知识上下文同时可见；
- 低频管理继续使用完整管理中心，高频操作不使用阻断式弹窗或 iframe；
- 浏览器只访问 DSH 同源地址，藏知 API 与预览由 DSH Host 代理，不直接暴露浏览器 localhost 端口。

用户明确希望后续开发直接推进到可交付结果，除非遇到会改变产品方向的阻塞，不要反复询问。

## 2. 当前实现状态

### 已实现

- Host 端同源 API 代理：`/_dsh-cangzhi-api/*` 转发到藏知 `/api/*`；
- Host 端 MCP 凭据代理仅监听 `127.0.0.1:3081`，自动添加 PAT 和当前知识空间请求头；
- DSH 首页原生藏知登录、PAT 创建和 MCP 连接流程；
- 14 个 `mcp__cangzhi__knowledge_*` 工具；
- DSH system prompt 中的藏知工具使用约束；
- 管理中心：概览、搜索、文档、网页收录、随手记、上传、分类、知识空间、对话接入；
- 新会话首页知识空间选择；
- 已有会话标题栏知识空间选择；
- 输入框上方知识增强状态和提问模板；
- MCP 搜索/问答结果的原生消息卡片、证据摘要和引用；
- 0.7.0 对话右侧常驻知识工作台：
  - 打开时通过 DSH 根布局的右侧 padding 收缩聊天区，不再显示遮罩背景；
  - 支持拖动左边缘调整 360–760px 宽度；
  - 使用 `localStorage[cangzhi-workbench-width]` 记忆宽度；
  - “资料 / 预览 / 当前对话”三个视图；
  - 搜索、最近资料、上传、PDF 预览；
  - 将资料标题和真实 document ID 写入当前会话草稿；
  - MCP 证据和回答引用点击后通过 `cangzhi-open-document` 事件在右侧预览，不再打开浏览器新窗口；
  - 窄屏下退化为右侧覆盖面板。

### 0.7.1 P0 修复

- 中等宽度窗口下，聊天区让出的右侧空间现在与工作台受视口约束后的实际宽度一致，不再留下空白或过度挤压对话；
- 工作台打开期间切换知识空间会重新加载资料，并清理旧空间的搜索、预览和临时固定资料；
- 拖拽结束会持久化最后一个 pointer 位置对应的宽度，不再偶尔保存前一帧宽度；
- 工作台加载阶段和 API 读取失败现在有明确状态，不再短暂显示误导性的空资料列表。

### 0.7.2 新会话入口与版面修复

- DSH 基座新增根作用域的 `conversation.hero.context` 列表槽位，位于新会话选择行和输入框之间；
- 藏知首页从已经失效的 `conversation.hero.extension` / `conversation.hero.headline` 注册迁移到该公开槽位；
- 新会话会显示藏知登录、连接和知识空间选择，同时保留 DSH 自己的代码工作区和智能体预设；
- 首页卡片收紧间距与层级，知识建议默认收起，移动端登录、操作按钮和会话标题栏选择器增加响应式布局；
- DSH 基座变更已补双语 Agent Note 和 resident composer 测试。

### 0.8.0 对话上下文与排版重构

- 新会话把 DSH 运行项目、智能体预设和藏知知识范围组合为统一的“对话上下文”面板，不再把不同概念平铺堆砌；
- “运行项目”明确只控制本地文件、命令目录、权限和 DSH 会话归档，“知识空间”只控制藏知检索与引用；
- 藏知首页移除资料数、分类数和处理数统计，首要操作收敛为空间选择、浏览资料、上传和管理；
- 首页、会话标题栏、知识增强区、右侧工作台、管理中心和工具证据卡统一提升到 DSH 的 12–14px 正文与控件字号；
- 新会话卡片说明两类上下文的关系，纯知识问答可固定使用同一个运行项目，涉及本地文件或代码时再切换。

### 0.7.x 尚未完整验收

0.8.0 已完成构建、安装和服务启动，但仍缺少可交互浏览器环境中的完整登录态验收。以下路径需要使用真实藏知管理员会话逐项验收：

- 登录后打开右侧工作台，确认聊天区实际收缩且 DSH 自有详情栏没有布局冲突；
- 搜索结果选择和 PDF blob 预览；
- 拖动宽度、关闭、重开后的宽度恢复；
- 上传后的列表刷新与处理状态；
- “加入对话”是否正确更新当前会话草稿；
- 模型实际调用 `knowledge_search` / `knowledge_ask` 后，点击证据或引用是否打开同一右侧预览；
- 760px 以下窄屏行为；
- 深色主题视觉。
- 新会话首页登录后切换知识空间，并确认发送首条消息前选择器持续可见。

不要把上述功能描述误认为已经通过端到端验收。

## 3. 关键代码入口

### 插件 Host

文件：`src/index.ts`

- `apply()` 注册 system prompt、Web/API 代理、状态、知识空间切换和 PAT 写入接口；
- `activeWorkspaceSlug` 是当前 MCP 代理使用的知识空间；
- `/_cangzhi-plugin/status` 返回 MCP 配置与当前空间；
- `/_cangzhi-plugin/workspace` 校验并切换空间；
- `/_cangzhi-plugin/token` 验证、保存或删除 DSH 凭据。

文件：`src/proxy.ts`

- 实现 HTTP/流式代理；
- 代理必须继续遵守 DSH connection 鉴权，不能开放匿名旁路。

### 插件 Client

文件：`src/client/plugin.tsx`

- `createConsoleFace()`：管理中心和知识工作台的开关状态；
- `HomeIntegration`：新会话首页登录、连接、统计、空间选择和上传；
- `KnowledgeDock`：输入框上方知识增强区；
- `ConversationKnowledgeHeader`：会话标题栏空间选择和工作台入口；
- `NativeWorkspace`：完整管理中心；
- `KnowledgeWorkbench`：0.7.0 右侧常驻工作台；
- `openDocumentInWorkbench()`：工具消息到工作台预览的浏览器事件桥；
- `EvidencePreview` / `CangzhiToolCard`：MCP 工具结果呈现；
- `apply()`：注册所有 DSH client slots。

文件：`src/client/Cangzhi.module.css`

- `[data-cangzhi-workbench='true']`：对 DSH 根 frame 增加右侧空间；
- `.knowledgeWorkbench` 及 `.workbench*`：常驻工作台布局；
- `.overlay` / `.workspace*`：低频完整管理中心；
- 文件中仍保留旧 `.drawer*` 样式，0.7.0 已不再渲染旧 `KnowledgeDrawer`，确认没有回退依赖后可以删除。

### 组装与启动

- `cordis.patch.yml`：Host 插件与 Cangzhi MCP server 组装；
- `scripts/setup-dsh.sh`：构建 DSH 扩展、构建插件并安装到 `web` Profile；
- `scripts/start-dsh.sh`：检查藏知 readiness 和端口后以前台方式启动 DSH；
- `scripts/rewrite-client-id.mjs`：构建后修正 client bundle 标识，不要漏跑；
- `README.md`：面向使用者的安装、启动、登录和常见问题文档。

## 4. 当前运行状态

交接时：

- 藏知 API readiness：`http://127.0.0.1:8000/api/readiness` 返回正常；
- DSH：`127.0.0.1:3080`；
- 内部 MCP 代理：`127.0.0.1:3081`；
- 正在运行的 DSH 进程启动参数为 `node apps/cli/lib/bin.js --profile web --host 127.0.0.1 --port 3080 --no-open`；
- Profile 已安装 `dsh-cangzhi@0.8.0`；
- 直接访问 DSH 根路径返回 401 是预期行为，必须使用每次启动日志中新生成的 `?token=...` URL。不要把启动 token 写进文档或代码。

进程和启动 token 都是临时状态。接手时先运行：

```bash
ss -ltnp | rg ':3080|:3081|:8000'
ps -eo pid,args | rg 'apps/cli/lib/bin.js.*profile web' | rg -v rg
```

## 5. 构建、安装与启动

### 更新后完整安装

```bash
export DSH_SOURCE=/home/percy/software/deepseek-harness
cd /data/share/cangzhi
./integrations/dsh-cangzhi/scripts/setup-dsh.sh
```

脚本已经覆盖：

1. DSH conversation UI bundle；
2. conversation UI TypeScript 检查；
3. 插件 Host/Client bundle；
4. `rewrite-client-id.mjs`；
5. JS 语法检查；
6. 重新安装到 `web` Profile。

### 启动

先正常停止已有 DSH 前台进程，再执行：

```bash
export DSH_SOURCE=/home/percy/software/deepseek-harness
cd /data/share/cangzhi
./integrations/dsh-cangzhi/scripts/start-dsh.sh
```

打开终端本次输出的 token URL。浏览器已有的旧 token URL 在 DSH 重启后不能继续使用。

### 最小验证

```bash
curl http://127.0.0.1:8000/api/readiness
node --check /data/share/cangzhi/integrations/dsh-cangzhi/lib/index.js
node --check /data/share/cangzhi/integrations/dsh-cangzhi/lib/client.js
```

## 6. 已执行的验证

本轮 0.8.0 执行并通过：

```text
pnpm --filter @deepseek-ai/dsh-client-ui-conversation run bundle
pnpm exec tsc -b packages/client/ui-conversation/tsconfig.json --pretty false
pnpm exec vitest run packages/client/ui-conversation/tests/skeleton.client.spec.tsx
tsdown --config integrations/dsh-cangzhi/tsdown.config.ts
node integrations/dsh-cangzhi/scripts/rewrite-client-id.mjs
node --check integrations/dsh-cangzhi/lib/index.js
node --check integrations/dsh-cangzhi/lib/client.js
./integrations/dsh-cangzhi/scripts/setup-dsh.sh
curl http://127.0.0.1:8000/api/readiness
```

DSH 和内部 MCP 代理均已监听。插件状态接口返回 API 已连接、MCP 已配置、14 个工具和默认知识空间。未执行完整仓库测试，也未完成 0.7.x 的登录态浏览器端到端验收。

## 7. 已知技术债与风险

按优先级排序：

1. **知识空间目前是 Host 进程全局状态。** `activeWorkspaceSlug` 对所有 DSH 会话共享；多浏览器、多用户或并行会话会互相影响。应改为会话级或请求级绑定，并保证 MCP 工具调用可以恢复对应的 session-visible 空间选择。
2. **“当前对话固定资料”目前只是 `KnowledgeWorkbench` 的 React 内存状态。** 切换会话时不会隔离，刷新后丢失，也没有作为模型可见的结构化会话状态持久化。下一步应接入 DSH session store/event，满足 DSH“model-visible 必须可回放”的约束。
3. **工作台通过给 DSH 根 frame 增加 padding 实现第四区域。** 这是不修改 DSH 基座的兼容做法，但要验证与 DSH 原生 details column 同时打开时的空间让渡。长期更合理的方案是给 DSH layout 增加可扩展的 secondary panel slot/service，而不是依赖 DOM 查询和 data attribute。
4. **`KnowledgeWorkbench` 的引用事件桥是浏览器事件。** 能完成 UI 联动，但没有 session ID；并行会话切换时可能把文档打开到当前可见会话。后续事件应携带并校验 session ID。
5. **大量 Client 中文文案仍直接写在 JSX 中。** DSH 当前规范要求 UI 文案进入 typed locale dictionaries。现有插件早期代码已经存在该问题，继续大改前应逐步迁移，避免将技术债扩大。
6. **旧 `.drawer*` CSS 仍在。** 完成回归后删除，减少 bundle 和维护歧义。
7. **缺少插件级自动化 UI 测试。** 当前主要依赖 build/typecheck 和人工浏览器验证。至少应增加：状态 face 测试、工作台开关/resize 测试、引用事件测试、登录态代理 smoke，以及真实 composition test。
8. **本地工作树是 dirty 状态。** 当前所有 `integrations/dsh-cangzhi` 修改都尚未提交；不要 reset 或覆盖。先阅读 `git diff`，保留已有修改。

## 8. 推荐后续开发顺序

### P0：完成 0.7.x 验收和修复

- 使用真实登录态走完第 2 节的所有未验收路径；
- 在 1440px、1024px、760px 和手机宽度检查布局；
- 同时打开 DSH 原生工具详情栏，检查四区域空间让渡；
- 检查深色主题、超长标题、空结果、无预览、API 失败和上传处理中状态；
- 修复后补最小自动化测试。

### P1：真正的会话知识上下文

- 将知识空间选择变成 DSH session-owned state；
- 将固定文档列表变成按 session 隔离、持久化、可回放的事件；
- 模型请求构造时明确注入空间与固定 document IDs；
- 新建会话必须在发送首条消息前显示空间选择；
- 切换会话时工作台同步恢复该会话的空间和固定资料。

### P2：DSH 原生布局扩展

- 优先评估在 DSH `ui-layout` 中增加 additive secondary panel 注册机制；
- 让插件通过公开 layout service 打开、关闭和调整右侧知识面板；
- 不要替换 `conversation` 或 `details` 的 single slot，否则会破坏 DSH 原生会话/工具详情能力；
- 保持桌面常驻分栏、窄屏覆盖降级。

### P3：完整知识工作台体验

- 左侧资源浏览器增加空间、分类、最近资料和保存的搜索；
- 预览支持页码、段落/片段定位和引用高亮，而不仅是打开 PDF；
- 搜索结果支持多选加入对话；
- 底部任务面板显示上传、解析、向量化、重试和错误；
- 管理中心逐步从全屏 overlay 迁移为 DSH 原生工作区或路由，但登录、危险确认等操作仍可使用 modal。

## 9. DSH 开发约束

修改 `/home/percy/software/deepseek-harness` 前必须阅读：

- `/home/percy/software/deepseek-harness/AGENTS.md`；
- 对应 package 目录向上的所有 `AGENTS.md`；
- `/home/percy/software/deepseek-harness/docs/architecture.md`。

特别注意：

- 产品能力优先通过插件和公开 slot/service 扩展；
- 不要为了藏知直接替换 DSH `conversation`、`sidebar` 或 `details` single slot；
- model-visible 状态必须进入 session log 并可回放；
- Client UI 文案应归属 locale dictionary；
- 非平凡 DSH 基座修改需要 Agent Note、相关测试和文档；
- 不要提交 token、Cookie、PAT 或 `.env` 凭据。

## 10. 交接检查清单

下一位开发者开始前：

- [ ] 阅读本文件和 `README.md`；
- [ ] 执行 `git status --short` 与 `git diff -- integrations/dsh-cangzhi`；
- [ ] 确认藏知 API、worker、DSH 和 MCP 代理状态；
- [ ] 获取当前启动日志中的新 token URL；
- [ ] 使用真实管理员账号登录并完成 0.7.x P0 验收；
- [ ] 先修复回归，再进行 session state 或 DSH layout 的结构升级；
- [ ] 每次修改后重新运行 `setup-dsh.sh` 并重启 DSH；
- [ ] 在交付说明中区分构建通过、人工验证通过和仍未验证的功能。
