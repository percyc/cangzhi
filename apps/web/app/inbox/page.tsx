'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

type DocumentItem = {
  id: number;
  title: string;
  source_type: string;
  updated_at: string;
  primary_category: { id: number; slug: string; name: string } | null;
  origin: {
    kind: string;
    label: string;
    connector_available: boolean;
    source_status: string | null;
  } | null;
  pipeline: Pipeline;
};

type Stage = {
  status: string;
  message: string;
  last_error?: string | null;
};

type Pipeline = {
  document_id: number;
  overall_status: 'processing' | 'completed' | 'failed';
  keyword_searchable: boolean;
  vector_searchable: boolean;
  stages: {
    parsing: Stage;
    understanding: Stage;
    chunking: Stage & { child_chunks: number };
    embedding: Stage & {
      model: string | null;
      completed: number;
      total: number;
      failed: number;
      missing: number;
    };
  };
};

type Filter =
  | 'attention'
  | 'all'
  | 'processing'
  | 'failed'
  | 'needs_organization'
  | 'source_issue'
  | 'not_vectorized'
  | 'completed';

type InboxCounts = {
  attention: number;
  all: number;
  processing: number;
  failed: number;
  needs_organization: number;
  source_issue: number;
  not_vectorized: number;
  completed: number;
};

type InboxResponse = {
  items: DocumentItem[];
  total: number;
  counts: InboxCounts;
  has_processing: boolean;
};

type RepairResult = {
  enqueued?: number;
  reset?: number;
};

const PAGE_SIZE = 25;
const POLL_INTERVAL = 3000;
const REQUEST_TIMEOUT = 20000;

const EMPTY_COUNTS: InboxCounts = {
  attention: 0,
  all: 0,
  processing: 0,
  failed: 0,
  needs_organization: 0,
  source_issue: 0,
  not_vectorized: 0,
  completed: 0,
};

export default function InboxPage() {
  const [items, setItems] = useState<DocumentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<InboxCounts>(EMPTY_COUNTS);
  const [filter, setFilter] = useState<Filter>('attention');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [repairing, setRepairing] = useState(false);

  const mountedRef = useRef(false);
  const visibleRef = useRef(true);
  const hasProcessingRef = useRef(false);
  const aborterRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const pollTimerRef = useRef<number | null>(null);
  const mutationRef = useRef(false);
  const performLoadRef = useRef<() => Promise<void>>(async () => {});
  const stateRef = useRef({ filter, page });
  useEffect(() => {
    stateRef.current = { filter, page };
  }, [filter, page]);

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const cancelLoad = useCallback(() => {
    ++generationRef.current;
    aborterRef.current?.abort();
    aborterRef.current = null;
  }, []);

  const performLoad = useCallback(async () => {
    if (!mountedRef.current || mutationRef.current) return;
    clearPollTimer();
    if (aborterRef.current) aborterRef.current.abort();
    const controller = new AbortController();
    aborterRef.current = controller;
    const gen = ++generationRef.current;
    setLoading(true);
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT);
    try {
      const { filter: f, page: p } = stateRef.current;
      const offset = Math.max(0, (p - 1) * PAGE_SIZE);
      const url =
        `/api/documents/inbox?filter=${encodeURIComponent(f)}` +
        `&offset=${offset}&limit=${PAGE_SIZE}`;
      const res = await fetch(url, { cache: 'no-store', signal: controller.signal });
      if (gen !== generationRef.current) return;
      if (!res.ok) throw new Error('收件箱读取失败');
      const data = (await res.json()) as InboxResponse;
      if (gen !== generationRef.current) return;
      const newTotal = Number(data?.total) || 0;
      setItems(Array.isArray(data?.items) ? data.items : []);
      setTotal(newTotal);
      setCounts({ ...EMPTY_COUNTS, ...(data?.counts ?? {}) });
      hasProcessingRef.current = Boolean(data?.has_processing);
      setError('');
      const totalPages = Math.max(1, Math.ceil(newTotal / PAGE_SIZE));
      if (stateRef.current.page > totalPages) {
        setPage(totalPages);
      }
    } catch (reason) {
      if (gen !== generationRef.current) return;
      const isAbort = reason instanceof DOMException && reason.name === 'AbortError';
      if (isAbort && !timedOut) {
        // aborted by navigation / filter / page change / unmount — silent
      } else if (isAbort && timedOut) {
        setError('收件箱请求超过 20 秒未返回，请重试。');
      } else {
        setError(reason instanceof Error ? reason.message : '收件箱读取失败');
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (gen === generationRef.current) {
        setLoading(false);
        aborterRef.current = null;
        if (mountedRef.current && visibleRef.current && hasProcessingRef.current && !mutationRef.current) {
          clearPollTimer();
          pollTimerRef.current = window.setTimeout(() => {
            pollTimerRef.current = null;
            if (!mountedRef.current) return;
            if (!visibleRef.current) return;
            if (!hasProcessingRef.current) return;
            void performLoadRef.current();
          }, POLL_INTERVAL);
        }
      }
    }
  }, [clearPollTimer]);

  useEffect(() => {
    performLoadRef.current = performLoad;
  }, [performLoad]);

  useEffect(() => {
    mountedRef.current = true;
    visibleRef.current = document.visibilityState === 'visible';
    return () => {
      mountedRef.current = false;
      cancelLoad();
      clearPollTimer();
    };
  }, [clearPollTimer, cancelLoad]);

  useEffect(() => {
    if (!mountedRef.current) return;
    clearPollTimer();
    void performLoad();
  }, [filter, page, performLoad, clearPollTimer]);

  useEffect(() => {
    const handle = () => {
      const visible = document.visibilityState === 'visible';
      visibleRef.current = visible;
      if (visible) {
        if (!mountedRef.current) return;
        clearPollTimer();
        aborterRef.current?.abort();
        void performLoad();
      } else {
        ++generationRef.current;
        aborterRef.current?.abort();
        aborterRef.current = null;
        clearPollTimer();
      }
    };
    document.addEventListener('visibilitychange', handle);
    return () => document.removeEventListener('visibilitychange', handle);
  }, [performLoad, clearPollTimer]);

  const currentPageParseFailures = useMemo(
    () =>
      items.filter((d) => {
        const s = d.pipeline.stages;
        return (
          s.parsing.status === 'failed' ||
          s.understanding.status === 'failed' ||
          s.chunking.status === 'failed'
        );
      }),
    [items],
  );

  const currentPageMissingVectors = useMemo(
    () =>
      items.filter(
        (d) =>
          d.pipeline.stages.embedding.status !== 'disabled' &&
          d.pipeline.stages.embedding.missing > 0,
      ),
    [items],
  );

  const busy = loading || Boolean(error);
  const canAct = !busy && !retrying && !repairing;

  const retryFailed = async () => {
    if (!canAct) return;
    if (currentPageParseFailures.length === 0) return;
    setRetrying(true);
    mutationRef.current = true;
    ++generationRef.current;
    setFeedback('');
    aborterRef.current?.abort();
    clearPollTimer();
    let succeeded = false;
    try {
      const results = await Promise.allSettled(
        currentPageParseFailures.map(async (d) => {
          const r = await fetch(`/api/documents/${d.id}/reprocess`, {
            method: 'POST',
          });
          if (!r.ok) throw new Error(`资料 ${d.id} 重试提交失败`);
        }),
      );
      const failed = results.find((result) => result.status === 'rejected');
      if (failed?.status === 'rejected') throw failed.reason;
      succeeded = true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量重试失败');
    } finally {
      mutationRef.current = false;
      setRetrying(false);
      if (succeeded) await performLoad();
    }
  };

  const repairMissingVectors = async () => {
    if (!canAct) return;
    if (currentPageMissingVectors.length === 0) return;
    setRepairing(true);
    mutationRef.current = true;
    ++generationRef.current;
    setFeedback('');
    aborterRef.current?.abort();
    clearPollTimer();
    let succeeded = false;
    try {
      const response = await fetch('/api/documents/batch/repair-vectors', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          document_ids: currentPageMissingVectors.map((d) => d.id),
        }),
      });
      if (!response.ok) throw new Error('补建缺失向量失败');
      const result = (await response.json()) as RepairResult;
      const enqueued = result.enqueued ?? 0;
      const reset = result.reset ?? 0;
      setFeedback(
        `已补建 ${enqueued} 项缺失向量并重置 ${reset} 项状态，正在重新处理…`,
      );
      succeeded = true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '补建缺失向量失败');
    } finally {
      mutationRef.current = false;
      setRepairing(false);
      if (succeeded) await performLoad();
    }
  };

  const filters: Array<{ key: Filter; label: string }> = [
    { key: 'attention', label: '需要关注' },
    { key: 'all', label: '全部' },
    { key: 'processing', label: '处理中' },
    { key: 'failed', label: '失败' },
    { key: 'needs_organization', label: '待整理' },
    { key: 'source_issue', label: '来源异常' },
    { key: 'not_vectorized', label: '未向量化' },
    { key: 'completed', label: '已完成' },
  ];

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const effectivePage = Math.min(page, totalPages);

  return (
    <main className="container mx-auto max-w-6xl p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">收件箱</h1>
          <p className="mt-1 text-sm text-slate-500">
            只处理需要关注的资料：处理中、失败、待整理和尚未建立语义索引的内容。
          </p>
          <p className="mt-1 text-xs text-slate-400">
            批量操作仅作用于当前页（每页 {PAGE_SIZE} 条），不会影响其他页。
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={retryFailed}
              disabled={!canAct || currentPageParseFailures.length === 0}
              className="rounded border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 disabled:opacity-40"
              title="仅对当前页解析或整理失败的资料重新提交处理"
              aria-label="重试当前页解析或整理失败"
            >
              {retrying
                ? '正在提交…'
                : `重试本页失败（${currentPageParseFailures.length}）`}
            </button>
            <button
              type="button"
              onClick={repairMissingVectors}
              disabled={!canAct || currentPageMissingVectors.length === 0}
              className="rounded bg-violet-600 px-4 py-2 text-sm text-white disabled:opacity-40"
              title="仅对当前页缺失向量的资料补建向量"
              aria-label="补建当前页缺失向量"
            >
              {repairing
                ? '正在补建…'
                : `本页补建缺失向量（${currentPageMissingVectors.length}）`}
            </button>
          </div>
        </div>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {filters.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => {
              if (filter === item.key) return;
              setItems([]); setLoading(true); setFilter(item.key); setPage(1);
            }}
            className={`rounded-full px-3 py-1.5 text-sm ${
              filter === item.key
                ? 'bg-slate-900 text-white'
                : 'border border-slate-300 bg-white text-slate-700'
            }`}
          >
            {item.label}（{counts[item.key]}）
          </button>
        ))}
      </div>

      {loading && items.length === 0 && (
        <p className="mt-6 text-sm text-slate-500">正在读取收件箱…</p>
      )}
      {error && (
        <p className="mt-4 text-sm text-red-700" role="alert">
          {error}
          <button type="button" onClick={() => void performLoad()} disabled={loading}
            className="ml-3 underline disabled:opacity-40">重新加载</button>
        </p>
      )}
      {feedback && <p className="mt-4 text-sm text-violet-700">{feedback}</p>}
      {!error && (
        <>
          <div className="mt-5 hidden overflow-x-auto rounded-xl border border-slate-200 bg-white sm:block">
            <table className="min-w-full text-left text-sm">
              <thead className="bg-slate-50 text-xs text-slate-500">
                <tr>
                  <th className="px-4 py-3">资料</th>
                  <th className="px-3 py-3">正文</th>
                  <th className="px-3 py-3">AI 整理</th>
                  <th className="px-3 py-3">切片</th>
                  <th className="px-3 py-3">向量</th>
                  <th className="px-3 py-3">可用状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((document) => {
                  const pipeline = document.pipeline;
                  const embedding = pipeline.stages.embedding;
                  return (
                    <tr key={document.id}>
                      <td className="max-w-xs px-4 py-3">
                        <Link
                          href={`/documents/${document.id}`}
                          className="font-medium text-slate-900 hover:underline"
                        >
                          {document.title}
                        </Link>
                      </td>
                      <StatusCell stage={pipeline.stages.parsing} />
                      <StatusCell stage={pipeline.stages.understanding} />
                      <StatusCell
                        stage={pipeline.stages.chunking}
                        suffix={`${pipeline.stages.chunking.child_chunks} 个`}
                      />
                      <StatusCell
                        stage={embedding}
                        suffix={
                          embedding.status === 'disabled'
                            ? '未启用'
                            : `${embedding.completed}/${embedding.total}${
                                embedding.missing > 0
                                  ? ` · 缺 ${embedding.missing}`
                                  : ''
                              }`
                        }
                      />
                      <td className="px-3 py-3 text-xs">
                        <p
                          className={
                            pipeline.keyword_searchable
                              ? 'text-emerald-700'
                              : 'text-slate-400'
                          }
                        >
                          {pipeline.keyword_searchable ? '可关键词检索' : '不可检索'}
                        </p>
                        <p
                          className={
                            pipeline.vector_searchable
                              ? 'text-violet-700'
                              : 'text-slate-400'
                          }
                        >
                          {pipeline.vector_searchable ? '可语义检索' : '语义未就绪'}
                        </p>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {items.length === 0 && !loading && (
              <p className="p-8 text-center text-sm text-slate-500">
                当前筛选下没有资料。
              </p>
            )}
          </div>
          <ul className="mt-5 flex flex-col gap-3 sm:hidden">
            {items.map((document) => {
              const pipeline = document.pipeline;
              const embedding = pipeline.stages.embedding;
              const progress =
                embedding.status !== 'disabled' && embedding.total > 0
                  ? Math.min(
                      100,
                      Math.max(0, (embedding.completed / embedding.total) * 100),
                    )
                  : null;
              const embeddingSuffix =
                embedding.status === 'disabled'
                  ? '未启用'
                  : `${embedding.completed}/${embedding.total}${
                      embedding.missing > 0 ? ` · 缺 ${embedding.missing}` : ''
                    }`;
              return (
                <li
                  key={document.id}
                  className="rounded-xl border border-slate-200 bg-white p-4"
                >
                  <Link
                    href={`/documents/${document.id}`}
                    className="block break-words text-sm font-medium text-slate-900 hover:underline"
                  >
                    {document.title}
                  </Link>
                  <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                    <div
                      title={
                        pipeline.stages.parsing.last_error ??
                        pipeline.stages.parsing.message
                      }
                    >
                      <dt className="text-slate-400">正文</dt>
                      <dd
                        className={`mt-0.5 ${statusColor(pipeline.stages.parsing.status)}`}
                      >
                        {pipeline.stages.parsing.message}
                      </dd>
                    </div>
                    <div
                      title={
                        pipeline.stages.understanding.last_error ??
                        pipeline.stages.understanding.message
                      }
                    >
                      <dt className="text-slate-400">AI 整理</dt>
                      <dd
                        className={`mt-0.5 ${statusColor(pipeline.stages.understanding.status)}`}
                      >
                        {pipeline.stages.understanding.message}
                      </dd>
                    </div>
                    <div
                      title={
                        pipeline.stages.chunking.last_error ??
                        pipeline.stages.chunking.message
                      }
                    >
                      <dt className="text-slate-400">切片</dt>
                      <dd
                        className={`mt-0.5 ${statusColor(pipeline.stages.chunking.status)}`}
                      >
                        {pipeline.stages.chunking.message}
                      </dd>
                      <p className="mt-0.5 text-slate-400">
                        {`${pipeline.stages.chunking.child_chunks} 个`}
                      </p>
                    </div>
                    <div
                      title={embedding.last_error ?? embedding.message}
                    >
                      <dt className="text-slate-400">向量</dt>
                      <dd className={`mt-0.5 ${statusColor(embedding.status)}`}>
                        {embedding.message}
                      </dd>
                      <p className="mt-0.5 text-slate-400">{embeddingSuffix}</p>
                    </div>
                  </dl>
                  {progress !== null && (
                    <div
                      className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-200"
                      role="progressbar"
                      aria-valuenow={Math.round(progress)}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label="向量建索引进度"
                    >
                      <div
                        className="h-1.5 rounded-full bg-violet-600 transition-[width]"
                        style={{ width: `${progress}%` }}
                      />
                    </div>
                  )}
                  <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                    <span
                      className={
                        pipeline.keyword_searchable
                          ? 'text-emerald-700'
                          : 'text-slate-400'
                      }
                    >
                      {pipeline.keyword_searchable ? '可关键词检索' : '不可检索'}
                    </span>
                    <span
                      className={
                        pipeline.vector_searchable
                          ? 'text-violet-700'
                          : 'text-slate-400'
                      }
                    >
                      {pipeline.vector_searchable ? '可语义检索' : '语义未就绪'}
                    </span>
                  </div>
                </li>
              );
            })}
            {items.length === 0 && !loading && (
              <li className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
                当前筛选下没有资料。
              </li>
            )}
          </ul>
        </>
      )}
      {!error && total > 0 && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-slate-600">
          <span>
            当前筛选共 {total} 条 · 第 {effectivePage}/{totalPages} 页
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={loading || effectivePage <= 1}
              onClick={() => { setItems([]); setLoading(true); setPage((current) => Math.max(1, current - 1)); }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 disabled:opacity-40"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={loading || effectivePage >= totalPages}
              onClick={() => { setItems([]); setLoading(true); setPage((current) => Math.min(totalPages, current + 1)); }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      )}
    </main>
  );
}

function statusColor(status: string): string {
  return status === 'completed'
    ? 'text-emerald-700'
    : status === 'failed'
      ? 'text-red-700'
      : ['processing', 'created', 'retry'].includes(status)
        ? 'text-blue-700'
        : 'text-slate-500';
}

function StatusCell({ stage, suffix }: { stage: Stage; suffix?: string }) {
  return (
    <td
      className={`px-3 py-3 text-xs ${statusColor(stage.status)}`}
      title={stage.last_error ?? stage.message}
    >
      <p>{stage.message}</p>
      {suffix && <p className="mt-0.5 text-slate-400">{suffix}</p>}
    </td>
  );
}
