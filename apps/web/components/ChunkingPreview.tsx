'use client';

import { useState } from 'react';
import { withApiBasePath } from '@/lib/paths';

type Preview = {
  calls: number; assisted_blocks: number; total_blocks: number;
  accepted_windows: number; failed_windows: number; provider_available: boolean;
  skipped_dataset: boolean;
  baseline: { children: number; short_children: number };
  candidate: { children: number; short_children: number };
  samples: { content: string; page: number | null; mode: string }[];
};

export default function ChunkingPreview({ documentId }: { documentId: string }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  async function preview(refresh = false) {
    setBusy(true); setError('');
    try {
      const response = await fetch(withApiBasePath(`/api/documents/${documentId}/chunking-preview${refresh ? '?refresh=true' : ''}`), { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '生成候选失败');
      setResult(data);
    } catch (e) { setError(e instanceof Error ? e.message : '生成候选失败'); }
    finally { setBusy(false); }
  }
  return <details className="mt-3 rounded-xl border border-slate-200 bg-white p-4">
    <summary className="cursor-pointer text-sm font-semibold text-slate-800">AI 辅助切片 · 候选评估</summary>
    <p className="mt-3 text-sm text-slate-600">生成候选不会替换当前索引或旧引用。点击后将有限原文窗口发送给已配置的对话模型，最多调用两次，其余部分沿用规则。</p>
    <button type="button" disabled={busy} onClick={() => void preview()} className="mt-3 rounded-lg border px-3 py-2 text-sm disabled:opacity-50">
      {busy ? '正在分析原文边界…' : '生成并比较候选'}
    </button>
    {result && <button type="button" disabled={busy} onClick={() => void preview(true)} className="ml-2 rounded-lg border px-3 py-2 text-sm disabled:opacity-50">重新生成</button>}
    {error && <p role="alert" className="mt-2 text-sm text-red-700">{error}</p>}
    {result && <div className="mt-3 space-y-2 text-sm text-slate-700" aria-live="polite">
      {result.skipped_dataset && <p>这是结构化数据集，不进行 AI 边界分析，继续使用数据集目录发现与精确查询。</p>}
      <p>默认规则基线 {result.baseline.children} 个片段 → 候选 {result.candidate.children} 个；不足 100 字：{result.baseline.short_children} → {result.candidate.short_children}。</p>
      <p>两者均按当前默认切片配置计算，不代表正在使用的索引数量。</p>
      <p>AI 已校验覆盖 {result.assisted_blocks}/{result.total_blocks} 个原文块，调用 {result.calls} 次，失败回退 {result.failed_windows} 个窗口。{!result.provider_available && '未配置可用对话模型，本次仅比较规则结果。'}</p>
      <p className="text-amber-800">尚未启用。片段更少不代表检索更好，需核对原文完整性与实际问题的召回效果。</p>
      <p>以下最多展示 5 个样例，每个截取前 900 字，并非完整片段。</p>
      {result.samples.map((sample, index) => <details key={index} className="rounded border p-2">
        <summary className="cursor-pointer">候选样例 {index + 1} · {sample.mode === 'ai' ? 'AI 边界' : '规则边界'}{sample.page != null ? ` · 第 ${sample.page} 页` : ''}</summary>
        <pre className="mt-2 whitespace-pre-wrap font-sans">{sample.content}</pre>
      </details>)}
    </div>}
  </details>;
}
