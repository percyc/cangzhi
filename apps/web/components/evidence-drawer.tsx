'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

import { withApiBasePath } from '@/lib/paths';

export type EvidenceCitation = {
  id: number;
  document_id: number;
  document_version_id: number;
  chunk_id: number;
  title: string;
  heading_path: string[];
  page: number | null;
  paragraph_index: number | null;
  source_type: string;
  source_url: string | null;
  snippet: string;
  evidence_type?: string;
  dataset_id?: number | null;
  artifact_version?: number | null;
  sheet_name?: string | null;
  region_index?: number | null;
  columns?: string[];
  query_plan?: Record<string, unknown>;
  source_rows?: number[];
  match_rows?: number | null;
  truncated?: boolean;
  aggregate?: Record<string, unknown>;
  table_location?: {
    sheet_name?: string;
    region_index?: number;
    row_start?: number;
    row_end?: number;
    column_names?: string[];
  };
};

type EvidenceContext = {
  evidence_type: string;
  document_id: number;
  document_version_id: number;
  title: string;
  heading_path: string[];
  page: number | null;
  snippet: string;
  context_markdown: string;
  preview_url: string | null;
  original_url: string | null;
  document_type: string;
  dataset?: {
    dataset_id: number;
    artifact_version: number | null;
    name: string;
    sheet_name: string;
    region_index: number;
    row_count: number;
    column_count: number;
  };
};

type EvidenceRows = {
  rows: Array<Record<string, unknown> & { row_number: number }>;
  columns: string[];
  returned: number;
  requested: number;
  truncated: boolean;
};

export function EvidenceDrawer({
  citation,
  onClose,
  active = true,
}: {
  citation: EvidenceCitation | null;
  onClose: () => void;
  active?: boolean;
}) {
  if (!citation) return (
    <section aria-label="引用证据" className="flex h-full flex-col items-center justify-center gap-3 bg-slate-50 p-6 text-center">
      <p className="text-sm font-semibold text-slate-700">在回答旁核对原文</p>
      <p className="max-w-xs text-xs leading-6 text-slate-500">点击回答中的引用编号，原文、文档版本或精确数据行会显示在这里；不会离开当前问答。</p>
      <button type="button" onClick={onClose} className="rounded-lg border px-3 py-2 text-xs text-slate-600">收起证据</button>
    </section>
  );
  // A different citation (including different contributing rows) starts a clean
  // reader. Collapsing the panel does not unmount it or lose its scroll/view.
  return <EvidenceReader key={JSON.stringify(citation)} citation={citation} onClose={onClose} active={active} />;
}

function EvidenceReader({ citation, onClose, active }: { citation: EvidenceCitation; onClose: () => void; active: boolean }) {
  const [context, setContext] = useState<EvidenceContext | null>(null);
  const [rows, setRows] = useState<EvidenceRows | null>(null);
  const [loading, setLoading] = useState(Boolean(citation));
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const requestRevisionRef = useRef(0);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [view, setView] = useState<'parsed' | 'original'>(
    citation?.evidence_type === 'pdf_word' ? 'original' : 'parsed',
  );

  useEffect(() => {
    const controller = new AbortController();
    const revision = ++requestRevisionRef.current;
    const isCurrent = () => !controller.signal.aborted && revision === requestRevisionRef.current;
    void (async () => {
      try {
        const response = await fetch(
          `/api/v1/knowledge/evidence/by-chunk/${citation.chunk_id}?document_version_id=${citation.document_version_id}`,
          { cache: 'no-store', signal: controller.signal },
        );
        if (!response.ok) throw new Error(await apiError(response, '证据读取失败'));
        const resolved = (await response.json()) as EvidenceContext;
        if (!isCurrent()) return;
        if (resolved.document_id !== citation.document_id || resolved.document_version_id !== citation.document_version_id) throw new Error('证据版本与引用不一致，请重新打开引用核对。');
        setContext(resolved);
        const datasetId = citation.dataset_id ?? resolved.dataset?.dataset_id;
        const sourceRows = citation.source_rows ?? [];
        if (datasetId && sourceRows.length > 0) {
          const rowResponse = await fetch(
            `/api/v1/knowledge/evidence/by-dataset/${datasetId}/rows?document_version_id=${citation.document_version_id}${citation.artifact_version ? `&artifact_version=${citation.artifact_version}` : ''}`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                source_rows: sourceRows,
                columns: citation.columns ?? citation.table_location?.column_names ?? [],
                limit: 200,
              }),
              signal: controller.signal,
            },
          );
          if (!rowResponse.ok) {
            throw new Error(await apiError(rowResponse, '原始数据行读取失败'));
          }
          const resolvedRows = (await rowResponse.json()) as EvidenceRows;
          if (!isCurrent()) return;
          setRows(resolvedRows);
        }
      } catch (reason) {
        if (isCurrent()) {
          setError(reason instanceof Error ? reason.message : '证据读取失败');
        }
      } finally {
        if (isCurrent()) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [citation, retry]);

  useEffect(() => {
    if (active) headingRef.current?.focus({ preventScroll: true });
  }, [active]);

  const originalUrl = useMemo(() => {
    if (!context || context.evidence_type !== 'pdf_word') return null;
    const raw =
      context.document_type === 'pdf'
        ? context.original_url ?? context.preview_url
        : context.preview_url ?? context.original_url;
    if (!raw) return null;
    // Embed only our version-bound PDF endpoints, never a website or executable URL.
    const safe = safePreviewPath(raw, context.document_id, context.document_version_id);
    if (!safe) return null;
    const mounted = withApiBasePath(safe);
    return mounted && context.page ? `${mounted}#page=${context.page}` : mounted;
  }, [context]);

  const isDataset =
    citation.evidence_type === 'dataset' || context?.evidence_type === 'dataset';

  return (
      <section aria-label="引用证据" className="flex h-full min-h-0 min-w-0 flex-col bg-white" onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-200 px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-3 sm:gap-4 sm:px-6 sm:py-4">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-blue-600">
              {isDataset ? '数据证据' : '原文证据'} · 版本 {citation.document_version_id}
            </p>
            <h2 ref={headingRef} tabIndex={-1} className="mt-1 break-words text-base font-semibold text-slate-900 outline-none">
              {citation.title}
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              {(context?.heading_path ?? citation.heading_path).join(' › ') || '正文'}
              {(context?.page ?? citation.page) ? ` · 第 ${context?.page ?? citation.page} 页` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            收起证据
          </button>
        </header>

        {context && !isDataset && originalUrl && (
          <nav className="flex shrink-0 gap-2 border-b border-slate-100 px-4 py-2 sm:px-6">
            <Tab active={view === 'original'} onClick={() => setView('original')}>原版预览</Tab>
            <Tab active={view === 'parsed'} onClick={() => setView('parsed')}>引用段落</Tab>
          </nav>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto break-words bg-slate-50/60 p-4 [overflow-wrap:anywhere] sm:p-5">
          {loading && <p className="text-sm text-slate-500">正在核对引用版本并读取证据…</p>}
          {error && <div role="alert" className="mb-3 rounded-xl bg-red-50 p-3 text-sm text-red-700"><p>{error}</p><button type="button" onClick={() => { requestRevisionRef.current += 1; setContext(null); setRows(null); setError(''); setLoading(true); setRetry((value) => value + 1); }} className="mt-2 rounded-lg border border-red-200 px-3 py-1.5 text-xs font-semibold">重试证据读取</button></div>}
          {context && !loading && !error && isDataset && <DatasetEvidence citation={citation} context={context} rows={rows} />}
          {context && !loading && !error && !isDataset && view === 'original' && originalUrl && (
            <div className="h-full min-h-[20rem] overflow-hidden rounded-2xl border border-slate-200 bg-white">
              <iframe title={`${citation.title} 原版预览`} src={originalUrl} className="h-full w-full" />
            </div>
          )}
          {context && !loading && !error && !isDataset && (view === 'parsed' || !originalUrl) && (
            <DocumentEvidence context={context} citation={citation} />
          )}
        </div>

        <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6 sm:py-3">
          <button
            type="button"
            onClick={onClose}
            className="text-xs font-semibold text-slate-700 sm:hidden"
          >
            ← 返回问答
          </button>
          <span className="hidden text-[11px] text-slate-400 sm:inline">证据固定到回答生成时的版本</span>
          <Link
            href={`/documents/${citation.document_id}?return_to=ask&chunk_id=${citation.chunk_id}${citation.page ? `&page=${citation.page}` : ''}`}
            className="text-xs font-semibold text-blue-700 hover:underline"
          >
            查看完整文档 →
          </Link>
        </footer>
      </section>
  );
}

function DocumentEvidence({
  context,
  citation,
}: {
  context: EvidenceContext;
  citation: EvidenceCitation;
}) {
  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
        <p className="mb-2 text-[11px] font-semibold text-amber-800">本次回答实际引用</p>
        <mark className="whitespace-pre-wrap bg-amber-200/80 text-sm leading-7 text-slate-900">
          {citation.snippet || context.snippet}
        </mark>
      </section>
      <section className="rounded-2xl border border-slate-200 bg-white p-5">
        <p className="mb-4 text-[11px] font-semibold text-slate-500">所在章节 / 段落</p>
        <div className="prose prose-slate max-w-none text-sm leading-7 [&_table]:block [&_table]:overflow-x-auto">
          <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={{
            img: ({ alt }) => <span className="text-xs text-slate-400">[图片未自动加载{alt ? `：${alt}` : ''}]</span>,
            a: ({ href, children }) => {
              const safe = safeHttpLink(href);
              return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="underline">{children}</a> : <span>{children}</span>;
            },
          }}>
            {context.context_markdown || context.snippet}
          </ReactMarkdown>
        </div>
      </section>
    </div>
  );
}

function DatasetEvidence({
  citation,
  context,
  rows,
}: {
  citation: EvidenceCitation;
  context: EvidenceContext;
  rows: EvidenceRows | null;
}) {
  const plan = citation.query_plan ?? {};
  const filters = Array.isArray(plan.filters) ? plan.filters : [];
  const groupBy = Array.isArray(plan.group_by) ? plan.group_by : [];
  return (
    <div className="space-y-4">
      <section className="grid gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-xs sm:grid-cols-3">
        <Meta label="数据集" value={context.dataset?.name ?? `#${citation.dataset_id ?? '-'}`} />
        <Meta label="工作表 / 区域" value={`${citation.sheet_name ?? context.dataset?.sheet_name ?? '-'} / ${citation.region_index ?? context.dataset?.region_index ?? '-'}`} />
        <Meta label="数据版本" value={`文档 v${citation.document_version_id} · 索引 v${citation.artifact_version ?? context.dataset?.artifact_version ?? '-'}`} />
      </section>
      <section className="rounded-2xl border border-blue-100 bg-blue-50/70 p-4">
        <h3 className="text-xs font-semibold text-blue-900">计算与筛选过程</h3>
        <div className="mt-3 space-y-2 text-xs leading-5 text-slate-700">
          <p>筛选：{filters.length ? filters.map(formatFilter).join('；') : '无'}</p>
          <p>分组：{groupBy.length ? groupBy.join('、') : '无'}</p>
          <p>计算：{metricLabel(String(plan.metric ?? 'rows'), plan.metric_column)}</p>
          <p>命中：{citation.match_rows ?? citation.source_rows?.length ?? 0} 行</p>
        </div>
        {citation.aggregate && Object.keys(citation.aggregate).length > 0 && (
          <pre className="mt-3 overflow-x-auto rounded-xl bg-white/80 p-3 text-xs text-slate-700">
            {JSON.stringify(citation.aggregate, null, 2)}
          </pre>
        )}
      </section>
      <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
          <h3 className="text-xs font-semibold text-slate-700">参与结果的原始行列</h3>
          <span className="text-[11px] text-slate-400">
            {citation.source_rows?.length
              ? `原始行 ${citation.source_rows.join('、')}${citation.truncated ? '（证据行较多，当前显示受控样本）' : ''}`
              : '旧引用未记录精确行号'}
          </span>
        </div>
        {rows?.rows.length ? (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr><th className="px-3 py-2">原始行</th>{rows.columns.map((column) => <th key={column} className="px-3 py-2">{column}</th>)}</tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.rows.map((row) => (
                  <tr key={row.row_number}><td className="px-3 py-2 font-medium text-slate-500">{row.row_number}</td>{rows.columns.map((column) => <td key={column} className="max-w-72 px-3 py-2 text-slate-700">{formatValue(row[column])}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="p-4 text-xs leading-5 text-slate-500">
            {citation.source_rows?.length ? '该版本未返回对应的原始贡献行，请核对引用或重试读取。' : '该引用来自旧版回答，只能显示当时保存的片段；重新提问后会记录精确贡献行。'}
          </p>
        )}
      </section>
    </div>
  );
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return <button type="button" onClick={onClick} className={`rounded-lg px-3 py-1.5 text-xs font-medium ${active ? 'bg-slate-900 text-white' : 'text-slate-500 hover:bg-slate-100'}`}>{children}</button>;
}

function Meta({ label, value }: { label: string; value: string }) {
  return <div><p className="text-[10px] text-slate-400">{label}</p><p className="mt-1 font-medium text-slate-700">{value}</p></div>;
}

function formatFilter(value: unknown) {
  if (!value || typeof value !== 'object') return String(value);
  const item = value as Record<string, unknown>;
  return `${item.column ?? '?'} ${item.operator ?? item.op ?? '='} ${formatValue(item.value)}`;
}

function metricLabel(metric: string, column: unknown) {
  const labels: Record<string, string> = { rows: '返回明细', count: '计数', count_distinct: '去重计数', sum: '求和', avg: '平均值', min: '最小值', max: '最大值' };
  return `${labels[metric] ?? metric}${column ? `（${String(column)}）` : ''}`;
}

function formatValue(value: unknown) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

async function apiError(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: string | { message?: string } };
    return typeof body.detail === 'string' ? body.detail : body.detail?.message ?? fallback;
  } catch {
    return fallback;
  }
}

function safePreviewPath(raw: string, documentId: number, versionId: number): string | null {
  if (!raw.startsWith('/api/')) return null;
  try {
    const url = new URL(raw, 'https://cangzhi.invalid');
    if (url.origin !== 'https://cangzhi.invalid' || ![`/api/documents/${documentId}/preview`, `/api/documents/${documentId}/original`].includes(url.pathname)) return null;
    const versions = url.searchParams.getAll('version_id');
    return versions.length === 1 && versions[0] === String(versionId) ? `${url.pathname}${url.search}` : null;
  } catch { return null; }
}

function safeHttpLink(raw?: string): string | null {
  if (!raw) return null;
  try { const url = new URL(raw); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}
