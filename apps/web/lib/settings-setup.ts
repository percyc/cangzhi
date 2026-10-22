export const SETUP_STEPS = [
  { id: 'chat', title: '对话与整理', hint: '自动摘要、分类和问答' },
  { id: 'embedding', title: '语义检索', hint: '可选 · 按含义找资料' },
  { id: 'ocr', title: '图片文字识别', hint: '可选 · 扫描件兜底' },
  { id: 'ready', title: '开始使用', hint: '核对状态与下一步' },
] as const;

export function setupChannelState({
  enabled, dirty, tested, complete = true, paused = false,
}: { enabled: boolean; dirty: boolean; tested?: boolean | null; complete?: boolean; paused?: boolean }) {
  if (dirty) return { title: '有未保存的修改', tone: 'warning', hint: '草稿尚未生效，请返回对应步骤保存。' };
  if (!enabled) return { title: '未启用（可选）', tone: 'neutral', hint: '不影响保存原文和使用关键词检索。' };
  if (!complete) return { title: '配置未完整', tone: 'warning', hint: '所选用途仍缺少地址、模型或必要密钥，请返回该步骤补齐。' };
  if (paused) return { title: '已配置 · 外部识别未启用', tone: 'neutral', hint: '单文档最多外发页数为 0，文档处理不会调用外部 OCR。' };
  if (tested === true) return { title: '本次测试通过', tone: 'success', hint: '只验证本次请求，不保证所有模型任务或文档质量。' };
  if (tested === false) return { title: '本次测试未通过', tone: 'warning', hint: '已保存配置，需按错误提示检查连接与模型。' };
  return { title: '已配置 · 本次未验证', tone: 'neutral', hint: '保存不代表连接可用，可返回该步骤主动测试。' };
}
