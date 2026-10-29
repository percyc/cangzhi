import { SETTINGS_GROUPS, type SettingsSection } from './usability';

export type SettingsSearchEntry = {
  id: string;
  title: string;
  description: string;
  href: string;
  group: string;
  section?: SettingsSection;
  keywords: readonly string[];
};

// Public product vocabulary only. Never add configuration values, user content,
// API responses or recent-query storage to this directory.
const SETTINGS_KEYWORDS: Record<SettingsSection, readonly string[]> = {
  setup: ['第一次使用', '新手', '开始配置', '引导', '向导', '配置向导', '如何配置', '初始化', '配置模型', '模型怎么配置', '怎么配置模型', 'openai接口'],
  chat: ['模型', '大模型', '对话', 'AI', 'openai', 'ollama', 'API地址', '密钥', 'api key', 'base url', '模型列表', '获取模型', '修改模型', '连接测试', '摘要', '自动分类', '自动整理', '标签生成'],
  embedding: ['向量', 'embedding', '语义检索', '索引', '向量模型', '重建索引', '构建索引', '启用索引', '回滚索引', '向量失败', '重试向量', '连接复用', '复用地址'],
  ocr: ['ocr', '扫描件', '扫描PDF', '图片', '识别文字', '图片转文字', '视觉模型', '乱码', '外部识别', '外发页数', '复用对话连接'],
  enhancement: ['知识增强', '增强', '知识图谱', '章节摘要', '实体关系', '模型预算', '增强预算'],
  sources: ['知识源', '来源', '数据库', '添加数据库', 'mysql', 'postgresql', 'webdav', 'webdev', '网盘', '导入表', '扫描目录', '扫描子目录', '刷新数据', '同步', '快照'],
  workspaces: ['空间', '知识空间', '工作空间', '新建空间', '项目隔离', '切换空间', '归档空间'],
  access: ['外部接入', 'mcp', 'cli', 'skill', 'pat', '令牌', 'token', '授权', '权限', '第三方', '外部AI', 'dsh', '访问密钥'],
  overview: ['运行概览', '监控', '统计', '磁盘', '硬盘', '容量', '磁盘占用', '硬盘占用', '硬盘占比', '文件数量', '切片数量', '向量数量', '任务积压', '并发', '模型并发', '模型额度', '模型池', '模型等待', 'worker'],
  uploads: ['上传限制', '上传上限', '上传大小', '上传太大', '上传文件太大', '上传多少', '文件大小', '文件太大', '50m', '50mb', '50mib', '413', 'nginx', '上传失败', '大小限制'],
  data: ['备份', '恢复备份', '导出', '数据导出', '下载全部', 'zip', '迁移系统', '恢复数据'],
  account: ['账户', '账号', '密码', '修改密码', '重置密码', '用户名', '改名', '登录设备', '退出设备', '会话', '安全'],
};

export const SETTINGS_SEARCH_ENTRIES: readonly SettingsSearchEntry[] = [
  ...SETTINGS_GROUPS.flatMap((group) => group.items.map((item) => ({
    id: item.id, title: item.title, description: item.hint,
    href: item.href, group: group.title, section: item.id,
    keywords: SETTINGS_KEYWORDS[item.id],
  }))),
  { id: 'documents', title: '知识库与文档预览', description: '浏览资料；打开详情核对原文、解析版与在线切片', href: '/documents', group: '使用知识', keywords: ['知识库', '文件预览', '原文', '文档', '解析版', '切片预览', '在线切片', '候选对比', '重新解析', '重新处理', '字段画像', '数据集', '查看文件'] },
  { id: 'search', title: '搜资料', description: '用关键词和范围查找资料，核对命中原文', href: '/search', group: '使用知识', keywords: ['搜资料', '搜索资料', '查找资料', '全文搜索', '关键词', '检索', '搜索文档'] },
  { id: 'ask', title: '问知识', description: '快速问答或深度分析，查看引用和数据集计算', href: '/ask', group: '使用知识', keywords: ['问知识', '提问', '问答', '对话', '聊天', '深度分析', '快速问答', '数据集统计', '历史问答', '引用'] },
  { id: 'upload-file', title: '上传文件', description: '添加 PDF、Word、WPS、Excel、Markdown、文本或 PNG/JPEG/WebP 图片', href: '/files/upload', group: '添加资料', keywords: ['上传', '上传文件', '添加文件', '导入文件', 'pdf', 'docx', 'wps', 'excel', 'xlsx', '图片', 'ocr', 'png', 'jpeg', 'webp'] },
  { id: 'new-note', title: '新建随手记', description: '记录 Markdown 笔记，保存原文后再整理', href: '/notes/new', group: '添加资料', keywords: ['笔记', '随手记', '记笔记', '新建笔记', 'markdown', '记录'] },
  { id: 'new-link', title: '收藏网页', description: '保存公开网页的正文和快照', href: '/links/new', group: '添加资料', keywords: ['网页', '链接', '收藏', '网址', '抓取', '文章'] },
  { id: 'inbox', title: '收件箱', description: '查看失败、待整理与需要关注的资料', href: '/inbox', group: '维护资料', keywords: ['收件箱', '待整理', '待治理', '解析失败', '处理失败', '重试失败', '入库状态'] },
  { id: 'processing', title: '处理中心', description: '查看解析、整理、切片和向量的处理进度', href: '/processing', group: '维护资料', keywords: ['处理中心', '进度', '队列', '任务', '解析进度', '待处理'] },
  { id: 'categories', title: '分类管理', description: '维护当前空间的分类层级', href: '/categories', group: '维护资料', keywords: ['分类', '分类管理', '修改分类', '新建分类', '目录'] },
  { id: 'tags', title: '标签管理', description: '重命名和合并标签，核对 AI 建议', href: '/tags', group: '维护资料', keywords: ['标签', '标签管理', '合并标签', '重命名标签', '同义词'] },
  { id: 'trash', title: '知识回收站', description: '在知识库切换“回收站”，恢复误删资料或确认永久删除', href: '/documents', group: '维护资料', keywords: ['回收站', '误删', '删除资料', '恢复文件', '永久删除'] },
];

export const SETTINGS_SEARCH_EXAMPLES = ['修改密码', '上传上限', '添加数据库', '模型'] as const;
export const SETTINGS_SEARCH_MAX_QUERY = 120;

function normalize(value: string): string {
  return value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}

export function searchSettings(query: string, limit = 8): SettingsSearchEntry[] {
  const bounded = query.slice(0, SETTINGS_SEARCH_MAX_QUERY);
  const needle = normalize(bounded);
  if (!needle) return [];
  const tokens = bounded.normalize('NFKC').toLowerCase().split(/\s+/).map(normalize).filter(Boolean);
  return SETTINGS_SEARCH_ENTRIES.map((entry, index) => {
    const title = normalize(entry.title);
    const aliases = entry.keywords.map(normalize);
    const corpus = [title, normalize(entry.description), ...aliases].join(' ');
    // Whole phrases outrank broad category matches. Reverse alias matching lets
    // common questions such as “在哪里修改密码” work without calling a model.
    let score = title === needle ? 120 : aliases.includes(needle) ? 100 : title.includes(needle) ? 90 : corpus.includes(needle) ? 70 : 0;
    for (const alias of aliases) {
      if (alias.length >= 2 && needle.includes(alias)) score = Math.max(score, 40 + Math.min(alias.length, 30));
    }
    if (tokens.length > 1 && tokens.every((token) => corpus.includes(token))) score = Math.max(score, 55);
    return { entry, score, index };
  }).filter((match) => match.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, Math.max(0, Math.min(SETTINGS_SEARCH_ENTRIES.length, Math.floor(limit))))
    .map((match) => match.entry);
}
