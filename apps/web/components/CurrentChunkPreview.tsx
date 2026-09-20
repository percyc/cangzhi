'use client';

import { useCallback, useState } from 'react';
import { withApiBasePath } from '@/lib/paths';

type ChunkItem = {
  id: number;
  order_index: number;
  chunk_type: string;
  page: number | null;
  heading_path: string[];
  char_count: number;
  content: string;
  truncated: boolean;
};

type ChunkQuality = {
  quality_level?: 'good' | 'review' | 'fallback';
  quality_score?: number;
  issues?: string[];
  headings_demoted?: number;
  model_calls?: number;
  short_children?: number;
};

type ChunkPage = {
  child_total: number;
  parent_total: number;
  offset: number;
  limit: number;
  quality: ChunkQuality | null;
  items: ChunkItem[];
};

const PAGE_SIZE = 5;

const issueLabels: Record<string, string> = {
  many_short_chunks: '短片段较多',
  dense_section_boundaries: '章节边界过密',
  repeated_headings: '存在重复标题',
  no_reliable_headings: '未确认可靠章节标题',
};

export default function CurrentChunkPreview({ documentId }: { documentId: string }) {
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<ChunkPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (nextOffset: number) => {
    setBusy(true);
    setError('');
    try {
      const response = await fetch(withApiBasePath(
        `/api/documents/${documentId}/chunks-preview?offset=${nextOffset}&limit=${PAGE_SIZE}`,
      ), { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '读取切片失败');
      setResult(data);
      setOffset(nextOffset);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '读取切片失败');
    } finally {
      setBusy(false);
    }
  }, [documentId]);

  const quality = result?.quality;
  const qualityText = quality?.quality_level === 'good'
    ? '结构稳定'
    : quality?.quality_level === 'fallback'
      ? '已使用安全兜底'
      : quality
        ? '建议抽查'
        : '等待下次重处理生成质量诊断';

  return <details
    className="border-t border-slate-100 px-4 py-3"
    onToggle={(event) => {
      if (event.currentTarget.open && !result && !busy) void load(0);
    }}
  >
    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm text-slate-600 hover:text-slate-900">
      <span className="font-medium">当前在线切片预览</span>
      <span className="shrink-0 text-xs text-slate-400">
        {result ? `${result.child_total} 个检索切片 · ${qualityText}` : '展开查看实际效果'}
      </span>
    </summary>
    <div className="mt-3 space-y-3 rounded-lg bg-slate-50 p-3">
      <p className="text-sm leading-6 text-slate-600">
        这里展示问答和搜索实际使用的切片，不调用模型，也不会修改索引。
      </p>
      {quality && <div className="flex flex-wrap gap-2 text-xs">
        <span className={`rounded-full px-2.5 py-1 ${quality.quality_level === 'good' ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-900'}`}>
          {qualityText}{quality.quality_score != null ? ` · ${quality.quality_score} 分` : ''}
        </span>
        {(quality.issues || []).map((issue) => <span key={issue} className="rounded-full bg-white px-2.5 py-1 text-slate-600">
          {issueLabels[issue] || issue}
        </span>)}
        {quality.headings_demoted ? <span className="rounded-full bg-white px-2.5 py-1 text-slate-600">已纠正 {quality.headings_demoted} 个误标题</span> : null}
        <span className="rounded-full bg-white px-2.5 py-1 text-slate-600">AI 调用 {quality.model_calls ?? 0}</span>
      </div>}
      {busy && <p className="text-sm text-slate-500">正在读取当前切片…</p>}
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
      {!busy && !error && result?.items.length === 0 && <p className="rounded-lg border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-500">当前没有可检索切片。</p>}
      {!busy && result && result.items.length > 0 && <div className="space-y-2">
        {result.items.map((item) => <details key={item.id} className="rounded-lg border border-slate-200 bg-white">
          <summary className="cursor-pointer px-3 py-2 text-sm text-slate-700">
            切片 {item.order_index + 1}
            {item.page ? ` · 第 ${item.page} 页` : ''}
            {item.heading_path.length > 0 ? ` · ${item.heading_path.join(' › ')}` : ''}
            <span className="ml-2 text-xs text-slate-400">{item.char_count} 字</span>
          </summary>
          <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap border-t border-slate-100 p-3 font-sans text-xs leading-6 text-slate-700">{item.content}{item.truncated ? '\n……预览已截断' : ''}</pre>
        </details>)}
        <div className="flex items-center justify-between pt-1 text-xs text-slate-500">
          <span>显示 {offset + 1}–{Math.min(offset + result.items.length, result.child_total)} / {result.child_total}</span>
          <div className="flex gap-2">
            <button type="button" disabled={busy || offset === 0} onClick={() => void load(Math.max(0, offset - PAGE_SIZE))} className="rounded border border-slate-300 bg-white px-2.5 py-1.5 disabled:opacity-40">上一页</button>
            <button type="button" disabled={busy || offset + PAGE_SIZE >= result.child_total} onClick={() => void load(offset + PAGE_SIZE)} className="rounded border border-slate-300 bg-white px-2.5 py-1.5 disabled:opacity-40">下一页</button>
          </div>
        </div>
      </div>}
    </div>
  </details>;
}
