'use client';

import { useState } from 'react';
import { withApiBasePath } from '@/lib/paths';

type Preview = {
  calls: number;
  assisted_blocks: number;
  total_blocks: number;
  accepted_windows: number;
  failed_windows: number;
  provider_available: boolean;
  skipped_dataset: boolean;
  baseline: { children: number; short_children: number };
  candidate: { children: number; short_children: number };
  samples: {
    content: string;
    page: number | null;
    mode: string;
    heading_path?: string[];
  }[];
};

type CurrentChunk = {
  id: number;
  order_index: number;
  page: number | null;
  heading_path: string[];
  char_count: number;
  content: string;
  truncated: boolean;
};

export default function ChunkingPreview({ documentId }: { documentId: string }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Preview | null>(null);
  const [current, setCurrent] = useState<CurrentChunk[]>([]);
  const [error, setError] = useState('');

  async function preview(refresh = false) {
    setBusy(true);
    setError('');
    try {
      const [response, currentResponse] = await Promise.all([
        fetch(withApiBasePath(`/api/documents/${documentId}/chunking-preview${refresh ? '?refresh=true' : ''}`), { method: 'POST' }),
        fetch(withApiBasePath(`/api/documents/${documentId}/chunks-preview?offset=0&limit=5`), { cache: 'no-store' }),
      ]);
      const [data, currentData] = await Promise.all([response.json(), currentResponse.json()]);
      if (!response.ok) throw new Error(data.detail || '生成候选失败');
      if (!currentResponse.ok) throw new Error(currentData.detail || '读取在线切片失败');
      setResult(data);
      setCurrent(currentData.items || []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '生成候选失败');
    } finally {
      setBusy(false);
    }
  }

  return <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-4 py-4 sm:px-6">
      <div>
        <h3 className="font-semibold text-slate-900">在线方案与 AI 候选</h3>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">
          供维护者评估切片策略。点击后只把有限原文窗口发送给已配置的对话模型，最多调用两次；
          不会替换在线切片、向量或旧引用，也不参与日常问答。
        </p>
      </div>
      <span className="rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800">诊断草案 · 不会启用</span>
    </div>
    <div className="p-4 sm:p-6">
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={() => void preview()} className="rounded-lg bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50">
          {busy ? '正在分析原文边界…' : '生成并对比候选'}
        </button>
        {result && <button type="button" disabled={busy} onClick={() => void preview(true)} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-50">忽略缓存并重新生成</button>}
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
      {result && <div className="mt-5 space-y-5 text-sm text-slate-700" aria-live="polite">
        {result.skipped_dataset && <p className="rounded-lg bg-blue-50 p-3 text-blue-800">这是结构化数据集，不进行 AI 边界分析，继续使用数据集目录发现与精确查询。</p>}
        <div className="grid gap-3 sm:grid-cols-3">
          <Metric label="检索片段（规则重算）" current={result.baseline.children} candidate={result.candidate.children} />
          <Metric label="不足 100 字" current={result.baseline.short_children} candidate={result.candidate.short_children} lowerIsBetter />
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs text-slate-500">AI 实际检查范围</p>
            <p className="mt-2 text-lg font-semibold text-slate-900">{result.assisted_blocks}<span className="text-sm font-normal text-slate-400"> / {result.total_blocks} 块</span></p>
          </div>
        </div>
        <p className="text-xs text-slate-500">
          模型调用 {result.calls} 次，失败回退 {result.failed_windows} 个窗口。
          {!result.provider_available && ' 未配置可用对话模型，本次仅比较规则结果。'}
        </p>
        <p className="rounded-lg bg-amber-50 p-3 text-amber-900">
          候选尚未启用；片段更少不代表检索更好。这些数字是按当前规则重算的对比，也不是在线索引实际占用量。
        </p>
        {result.samples.length > 0 && <div>
          <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
            <div>
              <h4 className="font-semibold text-slate-900">前 5 个边界顺序对照</h4>
              <p className="mt-1 text-xs text-slate-500">左右按文档顺序排列，用来观察标题归属、截断位置和片段长度；不是逐字差异。</p>
            </div>
            <div className="flex gap-4 text-xs font-medium">
              <span className="text-slate-600">左：当前在线</span>
              <span className="text-blue-700">右：候选草案</span>
            </div>
          </div>
          <div className="space-y-3">
            {Array.from({ length: Math.max(current.length, result.samples.length) }, (_, index) => {
              const online = current[index];
              const candidate = result.samples[index];
              return <div key={index} className="grid overflow-hidden rounded-xl border border-slate-200 lg:grid-cols-2">
                <SamplePane
                  index={index}
                  tone="current"
                  title={online?.heading_path?.join(' › ') || '当前在线切片'}
                  page={online?.page}
                  meta={online ? `${online.char_count} 字` : undefined}
                  content={online?.content}
                  truncated={online?.truncated}
                />
                <SamplePane
                  index={index}
                  tone="candidate"
                  title={candidate?.heading_path?.join(' › ') || '候选切片'}
                  page={candidate?.page}
                  meta={candidate ? (candidate.mode === 'ai' ? 'AI 建议边界' : '规则边界') : undefined}
                  content={candidate?.content}
                />
              </div>;
            })}
          </div>
        </div>}
      </div>}
    </div>
  </section>;
}

function Metric({ label, current, candidate, lowerIsBetter = false }: {
  label: string;
  current: number;
  candidate: number;
  lowerIsBetter?: boolean;
}) {
  const delta = candidate - current;
  const favorable = lowerIsBetter && delta < 0;
  return <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
    <p className="text-xs text-slate-500">{label}</p>
    <div className="mt-2 flex items-baseline gap-2">
      <span className="text-lg font-semibold text-slate-900">{current}</span>
      <span className="text-slate-300">→</span>
      <span className="text-lg font-semibold text-blue-700">{candidate}</span>
      {delta !== 0 && <span className={`text-xs ${favorable ? 'text-emerald-700' : 'text-slate-500'}`}>{delta > 0 ? '+' : ''}{delta}</span>}
    </div>
    <p className="mt-1 text-[11px] text-slate-400">规则基线 → 候选草案</p>
  </div>;
}

function SamplePane({ index, tone, title, page, meta, content, truncated = false }: {
  index: number;
  tone: 'current' | 'candidate';
  title: string;
  page?: number | null;
  meta?: string;
  content?: string;
  truncated?: boolean;
}) {
  const candidate = tone === 'candidate';
  return <article className={`${candidate ? 'border-t border-blue-100 bg-blue-50/30 lg:border-l lg:border-t-0' : 'bg-white'} min-w-0`}>
    <header className={`flex items-center justify-between gap-2 border-b px-3 py-2 ${candidate ? 'border-blue-100 bg-blue-50 text-blue-950' : 'border-slate-100 text-slate-800'}`}>
      <p className="min-w-0 truncate text-xs font-semibold"><span className="mr-2">{index + 1}</span>{title}</p>
      <span className="shrink-0 text-[11px] opacity-60">{page ? `第 ${page} 页` : ''}{page && meta ? ' · ' : ''}{meta}</span>
    </header>
    {content ? <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap p-3 font-sans text-xs leading-6 text-slate-700">{content}{truncated ? '\n……在线预览已截断' : ''}</pre> : <p className="p-4 text-xs text-slate-400">没有对应的第 {index + 1} 个片段</p>}
  </article>;
}
