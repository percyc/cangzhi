// Shared UI guidance only. Permissions, scopes and processing remain server-owned.
export const SETTINGS_GROUPS = [
  { title: '模型与检索', items: [
    { id: 'chat', href: '/settings?section=chat', title: '对话模型', hint: '摘要、分类与问答 · 可选' },
    { id: 'embedding', href: '/settings?section=embedding', title: '向量与索引', hint: '语义检索与索引版本 · 可选' },
    { id: 'ocr', href: '/settings?section=ocr', title: '图片文字识别', hint: '扫描 PDF 的外部识别 · 可选' },
    { id: 'enhancement', href: '/settings/enhancement', title: '知识增强', hint: '按预算补充理解 · 高级可选' },
  ] },
  { title: '资料与连接', items: [
    { id: 'sources', href: '/settings/sources', title: '知识源', hint: 'WebDAV 文件与数据库表' },
    { id: 'workspaces', href: '/settings/workspaces', title: '工作空间', hint: '隔离不同项目的知识资料' },
    { id: 'access', href: '/settings/access', title: '外部接入', hint: '连接 AI、CLI、Skill 与 MCP' },
  ] },
  { title: '系统与安全', items: [
    { id: 'data', href: '/settings/data', title: '数据与备份', hint: '导出资料与系统恢复边界' },
    { id: 'account', href: '/settings/account', title: '账户与安全', hint: '用户名、密码与登录设备' },
  ] },
] as const;

export type SettingsSection = typeof SETTINGS_GROUPS[number]['items'][number]['id'];
export type PageHelp = { title: string; steps: readonly string[]; note: string; action: { href: string; label: string } };

const HELP: Record<string, PageHelp> = {
  documents: { title: '保存、整理和查看资料', steps: ['添加文件、随手记或公开网页，先保存原文。', '用分类浏览资料，打开详情检查正文和切片。', '需要排查处理问题时，前往收件箱。'], note: '关键词检索不要求配置向量模型；删除资料先进入可恢复的回收站。', action: { href: '/files/upload', label: '上传资料' } },
  inbox: { title: '知道哪些资料需要关注', steps: ['先看“需要关注”，再按失败、待整理或向量状态筛选。', '打开一条资料查看具体阶段和原因。', '确认原因后再重试；批量按钮仅作用于当前页。'], note: '待整理不等于没有入库，也不等于不能检索；待治理表格需先整理结构。', action: { href: '/documents', label: '回到知识库' } },
  search: { title: '找原文，用搜索；要归纳，用问答', steps: ['输入关键词即可搜索，也可以先选择分类、标签或来源。', '不同筛选组同时生效，过多条件可能导致没有结果。', '展开命中片段核对原文，或把当前关键词带到问知识。'], note: '向量不可用时仍可关键词检索；搜索命中不代表资料的全部内容。', action: { href: '/ask', label: '前往问知识' } },
  ask: { title: '依据资料回答，而不是记住聊天', steps: ['先确认工作空间与知识范围，再输入问题。', '快速问答适合单个问题，深度分析适合多步查证或计算。', '核对回答引用；每次提问都独立检索。'], note: '数据集统计由程序精确查询；问答记录用于回看，不自动成为模型记忆。', action: { href: '/search', label: '改用搜资料' } },
  detail: { title: '先核对内容，再判断处理效果', steps: ['查看正文和知识库是否就绪，异常阶段可展开查看。', '在“内容与切片”切换原文、解析版或实际在线切片。', '数据集使用字段画像与原始行核对，复杂表格按治理建议整理。'], note: 'AI 切片候选只是诊断草案，不会自动替换在线索引。', action: { href: '/documents', label: '回到知识库' } },
  note: { title: '先记录，不必先分类', steps: ['填写正文，标题可以留空。', '保存后自动进入资料详情，后台继续整理和索引。', '模型暂不可用时仍会保留原文，可稍后补充整理。'], note: '关闭页面前先保存；本页不提供跨设备草稿。', action: { href: '/documents', label: '查看已有资料' } },
  edit: { title: '编辑正文，保留版本', steps: ['修改随手记标题或正文。', '保存后返回详情核对内容。', '实质修改会生成新版本，并重新处理派生内容。'], note: '尚未保存的正文仅在当前页面；离开前先保存。', action: { href: '/documents', label: '回到知识库' } },
  link: { title: '收藏公开网页的正文', steps: ['粘贴可公开访问的 http/https 网页地址。', '保存后到详情查看抓取与解析状态。', '无法公开访问的资料可改为上传文件或保存随手记。'], note: '不支持需要登录的页面、localhost 或受限内网地址。', action: { href: '/files/upload', label: '改用上传文件' } },
  upload: { title: '上传成功和处理完成是两件事', steps: ['选择支持的文件，可一次上传多个。', '确认每个文件的上传结果，失败项可以重试。', '到收件箱查看解析、整理、切片和向量是否完成。'], note: '原文件先保存；复杂 Excel 可能进入待治理，不强行生成统计。', action: { href: '/inbox', label: '查看处理结果' } },
  categories: { title: '分类用于组织，标签用于补充线索', steps: ['为长期主题建立分类层级，避免为每条资料新建分类。', '分类下已有资料时，先调整资料再删除分类。', '在资料详情或批量管理中纠正主分类。'], note: '分类是当前工作空间的资料组织方式，不是访问权限。', action: { href: '/documents', label: '整理资料' } },
  tags: { title: '让同义标签保持一致', steps: ['搜索现有标签，再决定是否重命名或合并。', '选择要合并的来源与保留的目标标签。', 'AI 建议只供参考，核对后再应用。'], note: '标签合并会更新资料关联；应用前确认保留哪个名称。', action: { href: '/documents', label: '查看资料' } },
  chat: { title: '想要摘要和问答？先配置这里', steps: ['选择 OpenAI 兼容或 Ollama；不使用 AI 也能保存资料。', '填写连接地址和密钥，按需获取模型列表，也可手输名称。', '保存并测试；成功后再使用摘要、分类和问答。'], note: '模型配置全局共享；留空密钥会保留已保存值，获取列表是可选步骤。', action: { href: '/notes/new', label: '先保存一条笔记' } },
  embedding: { title: '模型连接与生效索引分开管理', steps: ['填写向量模型连接，获取列表或手输名称。', '保存并测试，再构建新索引版本。', '构建完成后启用；原索引在切换前继续服务。'], note: '构建可能产生模型费用；失败先查原因，不必重传原文件。', action: { href: '/inbox', label: '查看向量状态' } },
  ocr: { title: '扫描 PDF 的可选外部识别', steps: ['普通 PDF 优先提取文字，扫描页先使用本地识别。', '只有需要额外识别时才配置外部视觉模型。', '设置外发页数上限，保存并测试。'], note: '外部 OCR 会发送候选图片页到模型服务并可能计费；不是所有 PDF 都需要。', action: { href: '/files/upload', label: '上传文件' } },
  enhancement: { title: '先用基础能力，再按需开启增强', steps: ['基础摘要、分类和切片不依赖此开关。', '按当前空间选择增强模块与调用预算。', '历史资料需显式发起，在文档详情查看进度和证据。'], note: '默认关闭；增强解释不是原文事实，也不自动替换检索索引。', action: { href: '/documents', label: '查看文档' } },
  sources: { title: '连接来源不等于已经导入', steps: ['选择 WebDAV 文件来源或数据库来源，填写只读连接。', '测试连接后扫描文件，或明确选择要导入的表。', '查看已导入资料与同步状态；数据库查询使用本地版本化快照。'], note: '刷新数据默认复用未变化的 AI 字段解释，只有需要时才选择全量重生成。', action: { href: '/documents', label: '查看已导入资料' } },
  workspaces: { title: '按项目隔离知识，不是创建用户', steps: ['资料相互独立时才新建空间；普通整理优先使用分类和标签。', '通过顶栏切换空间，再上传或提问。', '归档保留资料，需要时可以恢复。'], note: '单管理员与模型配置全局共享；工作空间不是团队权限系统。', action: { href: '/documents', label: '查看当前空间资料' } },
  data: { title: '资料导出与完整备份用途不同', steps: ['导出 ZIP 用于阅读或迁移资料。', '按需包含回收站和原文件，包体会变大。', '系统完整恢复需数据库、storage 与加密主密钥的联合备份。'], note: '导出不会删除原资料；服务器备份和恢复仍由维护者执行。', action: { href: '/documents', label: '回到知识库' } },
  access: { title: '让外部 AI 探索知识', steps: ['每个客户端创建独立令牌，明确选择工作空间。', '只读探索通常只需读取与检索权限。', '按接入示例配置 REST、CLI、Skill 或 MCP；不用时撤销令牌。'], note: '令牌明文只显示一次；普通登录会话和外部令牌分开管理。', action: { href: '/settings/workspaces', label: '管理知识范围' } },
  account: { title: '维护管理员与登录设备', steps: ['修改用户名或密码时验证当前密码。', '改密后当前设备继续登录，其他浏览器会话会退出。', '外部客户端令牌需在外部接入页单独撤销。'], note: '本页不创建其他用户或团队成员。', action: { href: '/settings/access', label: '管理外部令牌' } },
};

export function pageHelp(pathname: string, section = 'chat'): PageHelp | null {
  if (pathname === '/login' || pathname === '/setup' || pathname === '/') return null;
  if (pathname === '/settings') return HELP[section === 'embedding' || section === 'ocr' ? section : 'chat'];
  if (pathname.startsWith('/settings/')) return HELP[pathname.slice('/settings/'.length)] ?? null;
  if (/^\/documents\/[^/]+$/.test(pathname)) return HELP.detail;
  if (/^\/notes\/[^/]+\/edit$/.test(pathname)) return HELP.edit;
  const key = ({ '/notes/new': 'note', '/links/new': 'link', '/files/upload': 'upload', '/processing': 'inbox', '/sources': 'sources' } as Record<string, string>)[pathname] ?? pathname.slice(1);
  return HELP[key] ?? null;
}

export function activeNavigation(pathname: string, href: string): boolean {
  const group = pathname.startsWith('/notes/') || pathname.startsWith('/links/') || pathname === '/files/upload' || pathname === '/categories' || pathname === '/tags'
    ? '/documents' : pathname === '/processing' ? '/inbox' : pathname === '/sources' ? '/settings' : pathname;
  return group === href || group.startsWith(`${href}/`);
}

export function libraryEmptyState(trash: boolean, filtered: boolean) {
  if (trash) return { title: '回收站为空', description: '删除的资料会暂存在这里，可以恢复；永久删除不可恢复。', kind: 'trash' };
  if (filtered) return { title: '此分类下暂无资料', description: '知识库可能已有其他分类的资料。清除分类筛选即可查看全部，也可在资料详情中调整分类。', kind: 'filtered' };
  return { title: '还没有资料', description: '从一条随手记、一篇文章链接或一个文件开始建立当前空间的知识库。', kind: 'new' };
}

export function apiErrorMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== 'object' || !('detail' in body)) return fallback;
  const detail = body.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message;
  // Do not echo validation input values: they can contain private material.
  return fallback;
}

export function workspaceSlug(name: string, fallback: string): string {
  const derived = name.trim().toLowerCase().replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '').replace(/-+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
  return name.trim() ? derived || fallback : '';
}
