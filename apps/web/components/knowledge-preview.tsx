'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import { withApiBasePath } from '@/lib/paths';

export type KnowledgePreviewTarget = {
  documentId: number;
  versionId: number;
  chunkId: number;
  title: string;
  sourceType: string;
  sourceUrl: string | null;
  page: number | null;
};

type DatasetContext = {
  dataset_id: number;
  document_version_id: number;
  artifact_version: number | null;
  name: string;
  sheet_name: string;
  row_count: number;
  column_count: number;
  fields: Array<{ name: string; inferred_type: string; sample_values: unknown[] }>;
};

type PreviewContext = {
  document_id: number;
  document_version_id: number;
  evidence_type: string;
  document_type: string;
  title: string;
  heading_path: string[];
  page: number | null;
  snippet: string;
  context_markdown: string;
  preview_url: string | null;
  original_url: string | null;
  dataset?: DatasetContext;
};

export function knowledgePreviewKey(target: KnowledgePreviewTarget): string {
  return `${target.documentId}:${target.versionId}:${target.chunkId}`;
}

function safeHttpUrl(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined;
  try {
    const url = new URL(raw);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

function filePreviewUrl(context: PreviewContext): string | null {
  if (context.evidence_type !== 'pdf_word') return null;
  const raw = context.document_type === 'pdf' ? context.original_url : context.preview_url;
  if (!raw?.startsWith('/api/documents/')) return null;
  const url = new URL(raw, 'https://preview.invalid');
  // Only version-locked file endpoints are embeddable, never an application page
  // or a remote URL returned from stored content.
  if (![`/api/documents/${context.document_id}/preview`, `/api/documents/${context.document_id}/original`].includes(url.pathname) ||
      url.searchParams.get('version_id') !== String(context.document_version_id)) return null;
  return `${withApiBasePath(`${url.pathname}${url.search}`)}${context.page ? `#page=${context.page}` : ''}`;
}

async function readError(response: Response, fallback: string): Promise<string> {
  if (response.status === 401) return '登录已过期，请重新登录后重试。搜索列表仍保留。';
  if (response.status === 404 || response.status === 409) return '这份资料或引用版本已变化，请重新搜索；不会替换成其他版本的内容。';
  const body = await response.json().catch(() => null);
  return typeof body?.detail === 'string' ? body.detail : typeof body?.detail?.message === 'string' ? body.detail.message : fallback;
}

export function KnowledgePreview({ target, onClose, className = '' }: {
  target: KnowledgePreviewTarget;
  onClose: () => void;
  className?: string;
}) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ key: string; context: PreviewContext | null; error: string } | null>(null);
  const [fileViewKey, setFileViewKey] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const identity = knowledgePreviewKey(target);
  const requestKey = `${identity}:${attempt}`;
  const current = result?.key === requestKey ? result : null;
  const context = current?.context;

  useEffect(() => { headingRef.current?.focus({ preventScroll: true }); }, [identity]);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(`/api/v1/knowledge/evidence/by-chunk/${target.chunkId}?document_version_id=${target.versionId}`, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(await readError(response, '原文预览读取失败'));
        const body = await response.json() as PreviewContext;
        if (controller.signal.aborted) return;
        if (body.document_id !== target.documentId || body.document_version_id !== target.versionId) throw new Error('预览与所选资料版本不一致，请重新搜索。');
        setResult({ key: requestKey, context: body, error: '' });
      } catch (reason) {
        if (!controller.signal.aborted) setResult({ key: requestKey, context: null, error: reason instanceof Error ? reason.message : '原文预览读取失败' });
      }
    })();
    return () => controller.abort();
  }, [requestKey, target.chunkId, target.documentId, target.versionId]);

  const originalUrl = context ? filePreviewUrl(context) : null;
  const isFileView = fileViewKey === identity && Boolean(originalUrl);
  const sourceUrl = safeHttpUrl(target.sourceUrl);
  const body = context?.context_markdown || context?.snippet || '';
  const location = new URLSearchParams({ chunk_id: String(target.chunkId) });
  if (target.page) location.set('page', String(target.page));

  return (
    <section id="knowledge-preview" aria-label="资料阅读区" className={`flex min-w-0 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm ${className}`}
      onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
      <header className="shrink-0 border-b border-slate-200 bg-slate-50/80 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold tracking-wider text-cyan-700">{context?.dataset ? '数据预览' : '原文阅读'} · 版本 #{target.versionId}</p>
            <h2 ref={headingRef} tabIndex={-1} className="mt-1 break-words text-lg font-semibold text-slate-950 outline-none focus-visible:ring-2 focus-visible:ring-cyan-600">{target.title}</h2>
          </div>
          <button type="button" onClick={onClose} className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-xs text-slate-600 hover:bg-white" aria-label="收起预览并返回搜索列表">收起预览</button>
        </div>
        <p className="mt-2 text-xs leading-5 text-slate-500">
          {{ note: '随手记', file: '文件', url: '网页' }[target.sourceType] || '资料'}
          {context?.heading_path?.length ? ` · ${context.heading_path.join(' › ')}` : ''}
          {(context?.page ?? target.page) ? ` · 第 ${context?.page ?? target.page} 页` : ''}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Link href={`/ask?document_ids=${target.documentId}`} className="rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white hover:bg-slate-700">仅问这份资料</Link>
          <Link href={`/documents/${target.documentId}?${location}`} className="rounded-lg border border-slate-300 px-3 py-2 text-xs text-slate-600 hover:bg-white">完整文档与管理</Link>
          {sourceUrl && <a href={sourceUrl} target="_blank" rel="noreferrer noopener" className="rounded-lg px-3 py-2 text-xs text-slate-600 underline">打开来源 ↗</a>}
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-5" aria-busy={!current}>
        {!current && <p role="status" className="py-10 text-center text-sm text-slate-500">正在读取所选版本的原文…</p>}
        {current?.error && <div role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <p>{current.error}</p>
          <button type="button" onClick={() => setAttempt((value) => value + 1)} className="mt-3 rounded-lg border border-amber-300 px-3 py-2">重试预览</button>
        </div>}
        {context && <>
          {originalUrl && <div className="mb-4 flex gap-1 rounded-xl bg-slate-100 p-1" aria-label="原文展示方式">
            <button type="button" aria-pressed={!isFileView} onClick={() => setFileViewKey(null)} className={`rounded-lg px-3 py-2 text-xs ${!isFileView ? 'bg-white font-medium shadow-sm' : 'text-slate-500'}`}>命中原文</button>
            <button type="button" aria-pressed={isFileView} onClick={() => setFileViewKey(identity)} className={`rounded-lg px-3 py-2 text-xs ${isFileView ? 'bg-white font-medium shadow-sm' : 'text-slate-500'}`}>文件版式</button>
          </div>}
          {isFileView && originalUrl ? <>
            <iframe key={requestKey} title={`${target.title} 文件版式`} src={originalUrl} sandbox="allow-same-origin" className="h-[65vh] min-h-96 w-full rounded-xl border border-slate-200" />
            <p className="mt-2 text-xs text-slate-500">若浏览器无法显示文件，<a href={originalUrl} target="_blank" rel="noreferrer noopener" className="underline">单独打开此版本</a>。</p>
          </> : context.dataset ? <DatasetSample key={`${identity}:${context.dataset.dataset_id}:${context.dataset.artifact_version}`} dataset={context.dataset} versionId={target.versionId} /> : <>
            <p className="mb-4 rounded-lg bg-cyan-50 p-3 text-xs leading-5 text-cyan-900">这里展示搜索命中片段及可用的同章节上下文，不是全文摘要。完整阅读、原文件和整理操作请进入完整文档。</p>
            {body ? <article className="prose prose-slate max-w-none break-words text-sm leading-7 [&_pre]:overflow-x-auto [&_table]:block [&_table]:overflow-x-auto">
              <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} skipHtml components={{
                img: ({ alt }) => <span className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-500">[图片：{alt || '为保护隐私，预览不自动加载外部图片'}]</span>,
                a: ({ href, children }) => safeHttpUrl(href) ? <a href={safeHttpUrl(href)} target="_blank" rel="noreferrer noopener">{children} ↗</a> : <span>{children}</span>,
              }}>{body.slice(0, 40000)}</ReactMarkdown>
            </article> : <p className="text-sm text-slate-500">这段原文暂时没有可展示的正文。</p>}
            {body.length > 40000 && <p className="mt-4 text-xs text-amber-700">预览仅显示前 40,000 字符，请进入完整文档继续阅读。</p>}
          </>}
        </>}
      </div>
    </section>
  );
}

type SampleResult = {
  key: string;
  total: number;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  error: string;
};

function DatasetSample({ dataset, versionId }: { dataset: DatasetContext; versionId: number }) {
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<SampleResult | null>(null);
  const limit = 20;
  const key = `${dataset.dataset_id}:${versionId}:${dataset.artifact_version}:${offset}:${attempt}`;
  const current = result?.key === key ? result : null;

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        // The current-row endpoint supplies pagination and real source row IDs.
        // Values are then fetched through the version-bound evidence service.
        const response = await fetch(`/api/datasets/${dataset.dataset_id}/rows?offset=${offset}&limit=${limit}`, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(await readError(response, '数据预览读取失败'));
        const page = await response.json() as { dataset_id: number; offset: number; total: number; rows: Array<{ row_number: number }> };
        if (controller.signal.aborted) return;
        if (page.dataset_id !== dataset.dataset_id || page.offset !== offset) throw new Error('数据预览与所选表格或页码不一致');
        const sourceRows = page.rows.map((row) => row.row_number).filter((row) => Number.isInteger(row) && row > 0);
        const rowsResponse = await fetch(`/api/v1/knowledge/evidence/by-dataset/${dataset.dataset_id}/rows?document_version_id=${versionId}${dataset.artifact_version ? `&artifact_version=${dataset.artifact_version}` : ''}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
          body: JSON.stringify({ source_rows: sourceRows, columns: [], limit }),
        });
        if (!rowsResponse.ok) throw new Error(await readError(rowsResponse, '版本数据行读取失败'));
        const rows = await rowsResponse.json() as { dataset_id: number; document_version_id: number; columns: string[]; rows: Array<Record<string, unknown>> };
        if (controller.signal.aborted) return;
        if (rows.dataset_id !== dataset.dataset_id || rows.document_version_id !== versionId) throw new Error('数据行版本与所选资料不一致');
        setResult({ key, total: page.total, columns: rows.columns, rows: rows.rows, error: '' });
      } catch (reason) {
        if (!controller.signal.aborted) setResult({ key, total: 0, columns: [], rows: [], error: reason instanceof Error ? reason.message : '数据预览读取失败' });
      }
    })();
    return () => controller.abort();
  }, [dataset.dataset_id, dataset.artifact_version, key, offset, versionId]);

  return <div className="min-w-0">
    <div className="rounded-xl bg-cyan-50 p-3 text-sm text-cyan-950">
      <p className="font-medium">{dataset.name}</p>
      <p className="mt-1 text-xs">{dataset.sheet_name} · {dataset.row_count.toLocaleString('zh-CN')} 行 / {dataset.column_count} 列 · 文档版本 #{versionId}{dataset.artifact_version ? ` · 数据索引 v${dataset.artifact_version}` : ''}</p>
      <p className="mt-2 text-xs leading-5">这是已保存版本的原始数据行，不是实时源库或提问后的统计结果。精确筛选与计算可使用“仅问这份资料”。</p>
    </div>
    {!current && <p role="status" className="py-8 text-center text-sm text-slate-500">正在读取当前页的数据行…</p>}
    {current?.error && <div role="alert" className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <p>{current.error}，未显示其他表或页码的旧数据。</p>
      <button type="button" onClick={() => setAttempt((value) => value + 1)} className="mt-2 rounded border border-amber-300 px-3 py-1.5">重试当前页</button>
      {offset > 0 && <button type="button" onClick={() => setOffset(0)} className="ml-2 rounded border border-amber-300 px-3 py-1.5">返回第一页</button>}
    </div>}
    {current && !current.error && <>
      <div className="mt-4 max-w-full overflow-x-auto rounded-xl border border-slate-200">
        <table className="min-w-full whitespace-nowrap text-left text-xs"><caption className="sr-only">{dataset.name} 的版本绑定数据预览</caption>
          <thead className="bg-slate-50 text-slate-500"><tr><th className="px-3 py-2">原始行</th>{current.columns.map((column) => <th key={column} className="px-3 py-2">{column}</th>)}</tr></thead>
          <tbody>{current.rows.map((row, index) => <tr key={`${String(row.row_number)}:${index}`} className="border-t border-slate-100"><td className="px-3 py-2 text-slate-400">{String(row.row_number)}</td>{current.columns.map((column) => <td key={column} className="max-w-64 truncate px-3 py-2" title={cellValue(row[column])}>{cellValue(row[column])}</td>)}</tr>)}</tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        <span>{current.total ? `第 ${offset + 1}–${Math.min(offset + limit, current.total)} 行 / 共 ${current.total} 行` : '暂无数据行'}</span>
        <div className="flex gap-2"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))} className="rounded-lg border px-3 py-2 disabled:opacity-40">上一页数据</button><button type="button" disabled={offset + limit >= current.total} onClick={() => setOffset(offset + limit)} className="rounded-lg border px-3 py-2 disabled:opacity-40">下一页数据</button></div>
      </div>
    </>}
    <details className="mt-4 rounded-lg border border-slate-200 p-3"><summary className="cursor-pointer text-xs font-medium text-slate-600">查看字段类型</summary>
      <dl className="mt-3 grid gap-2 text-xs">{dataset.fields?.map((field) => <div key={field.name} className="flex flex-wrap justify-between gap-2"><dt className="break-all text-slate-700">{field.name}</dt><dd className="text-slate-400">{field.inferred_type}</dd></div>)}</dl>
    </details>
  </div>;
}

function cellValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
