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
  return <details className="border-t border-slate-100 px-4 py-3">
    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm text-slate-600 hover:text-slate-900">
      <span className="font-medium">高级诊断：AI 切片候选</span>
      <span className="shrink-0 text-xs text-slate-400">仅评估，不影响当前索引</span>
    </summary>
    <div className="mt-3 rounded-lg bg-slate-50 p-3">
      <p className="text-sm leading-6 text-slate-600">
        供维护者评估切片策略。只有点击后才会把有限原文窗口发送给已配置的对话模型，
        最多调用两次；不会替换在线切片、向量或旧引用，也不参与日常问答。
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={() => void preview()} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-50">
          {busy ? '正在分析原文边界…' : '运行候选对比'}
        </button>
        {result && <button type="button" disabled={busy} onClick={() => void preview(true)} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-50">忽略缓存并重新生成</button>}
      </div>
      {error && <p role="alert" className="mt-2 text-sm text-red-700">{error}</p>}
      {result && <div className="mt-4 space-y-3 text-sm text-slate-700" aria-live="polite">
        {result.skipped_dataset && <p className="rounded-md bg-blue-50 p-2 text-blue-800">这是结构化数据集，不进行 AI 边界分析，继续使用数据集目录发现与精确查询。</p>}
        <div className="grid gap-2 sm:grid-cols-3">
          <Metric label="片段数量" value={`${result.baseline.children} → ${result.candidate.children}`} />
          <Metric label="不足 100 字" value={`${result.baseline.short_children} → ${result.candidate.short_children}`} />
          <Metric label="AI 覆盖原文块" value={`${result.assisted_blocks}/${result.total_blocks}`} />
        </div>
        <p>
          模型调用 {result.calls} 次，失败回退 {result.failed_windows} 个窗口。
          {!result.provider_available && ' 未配置可用对话模型，本次仅比较规则结果。'}
        </p>
        <p className="rounded-md bg-amber-50 p-2 text-amber-900">
          候选尚未启用；片段更少不代表检索更好。这些数字是按当前规则重算的对比，
          也不是在线索引实际占用量。
        </p>
        {result.samples.length > 0 && <details className="rounded-md border border-slate-200 bg-white">
          <summary className="cursor-pointer px-3 py-2 font-medium text-slate-700">
            查看候选样例（最多 5 个，默认折叠）
          </summary>
          <div className="max-h-[28rem] space-y-2 overflow-y-auto border-t border-slate-100 p-3">
            {result.samples.map((sample, index) => <details key={index} className="rounded border border-slate-200 p-2">
              <summary className="cursor-pointer">候选样例 {index + 1} · {sample.mode === 'ai' ? 'AI 边界' : '规则边界'}{sample.page != null ? ` · 第 ${sample.page} 页` : ''}</summary>
              <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap font-sans text-xs leading-5 text-slate-600">{sample.content}</pre>
            </details>)}
          </div>
        </details>}
      </div>}
    </div>
  </details>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md border border-slate-200 bg-white p-2">
    <p className="text-xs text-slate-500">{label}</p>
    <p className="mt-1 font-semibold text-slate-800">{value}</p>
  </div>;
}
