'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';

type DocumentItem = {
  id: number;
  title: string;
  source_type: string;
  updated_at: string;
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
    };
  };
};

type Row = { document: DocumentItem; pipeline: Pipeline };
type Filter =
  | 'attention'
  | 'all'
  | 'processing'
  | 'failed'
  | 'not_vectorized'
  | 'completed';

export default function InboxPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [filter, setFilter] = useState<Filter>('attention');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(async () => {
    try {
      const documentsResponse = await fetch('/api/documents?limit=200', {
        cache: 'no-store',
      });
      if (!documentsResponse.ok) throw new Error('资料列表读取失败');
      const documents = (await documentsResponse.json()) as DocumentItem[];
      const statuses = await Promise.all(
        documents.map(async (document) => {
          const response = await fetch(
            `/api/documents/${document.id}/processing-status`,
            { cache: 'no-store' },
          );
          if (!response.ok) throw new Error(`资料 ${document.id} 状态读取失败`);
          return {
            document,
            pipeline: (await response.json()) as Pipeline,
          };
        }),
      );
      setRows(statuses);
      setError('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '读取处理中心失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (!rows.some((row) => row.pipeline.overall_status === 'processing')) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load, rows]);

  const counts = useMemo(
    () => ({
      attention: rows.filter(
        (row) =>
          row.pipeline.overall_status !== 'completed' ||
          !row.pipeline.vector_searchable,
      ).length,
      all: rows.length,
      processing: rows.filter((row) => row.pipeline.overall_status === 'processing')
        .length,
      failed: rows.filter((row) => row.pipeline.overall_status === 'failed').length,
      not_vectorized: rows.filter((row) => !row.pipeline.vector_searchable).length,
      completed: rows.filter((row) => row.pipeline.overall_status === 'completed')
        .length,
    }),
    [rows],
  );

  const visibleRows = rows.filter((row) => {
    if (filter === 'attention')
      return (
        row.pipeline.overall_status !== 'completed' ||
        !row.pipeline.vector_searchable
      );
    if (filter === 'all') return true;
    if (filter === 'not_vectorized') return !row.pipeline.vector_searchable;
    return row.pipeline.overall_status === filter;
  });

  const retryFailed = async () => {
    const failed = rows.filter((row) => row.pipeline.overall_status === 'failed');
    if (!failed.length) return;
    setRetrying(true);
    try {
      await Promise.all(
        failed.map(async ({ document }) => {
          const response = await fetch(`/api/documents/${document.id}/reprocess`, {
            method: 'POST',
          });
          if (!response.ok) throw new Error(`资料 ${document.id} 重试提交失败`);
        }),
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量重试失败');
    } finally {
      setRetrying(false);
    }
  };

  const filters: Array<{ key: Filter; label: string }> = [
    { key: 'attention', label: '需要关注' },
    { key: 'all', label: '全部' },
    { key: 'processing', label: '处理中' },
    { key: 'failed', label: '失败' },
    { key: 'not_vectorized', label: '未向量化' },
    { key: 'completed', label: '已完成' },
  ];

  return (
    <main className="container mx-auto max-w-6xl p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">收件箱</h1>
          <p className="mt-1 text-sm text-slate-500">
            只处理需要关注的资料：处理中、失败、待整理和尚未建立语义索引的内容。
          </p>
        </div>
        <button
          type="button"
          onClick={retryFailed}
          disabled={retrying || counts.failed === 0}
          className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40"
        >
          {retrying ? '正在提交…' : `重试全部失败项（${counts.failed}）`}
        </button>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {filters.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
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

      {loading && <p className="mt-6 text-sm text-slate-500">正在汇总处理状态…</p>}
      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
      {!loading && !error && (
        <div className="mt-5 overflow-x-auto rounded-xl border border-slate-200 bg-white">
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
              {visibleRows.map(({ document, pipeline }) => (
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
                    stage={pipeline.stages.embedding}
                    suffix={
                      pipeline.stages.embedding.status === 'disabled'
                        ? '未启用'
                        : `${pipeline.stages.embedding.completed}/${pipeline.stages.embedding.total}`
                    }
                  />
                  <td className="px-3 py-3 text-xs">
                    <p className={pipeline.keyword_searchable ? 'text-emerald-700' : 'text-slate-400'}>
                      {pipeline.keyword_searchable ? '可关键词检索' : '不可检索'}
                    </p>
                    <p className={pipeline.vector_searchable ? 'text-violet-700' : 'text-slate-400'}>
                      {pipeline.vector_searchable ? '可语义检索' : '语义未就绪'}
                    </p>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {visibleRows.length === 0 && (
            <p className="p-8 text-center text-sm text-slate-500">当前筛选下没有资料。</p>
          )}
        </div>
      )}
    </main>
  );
}

function StatusCell({ stage, suffix }: { stage: Stage; suffix?: string }) {
  const color =
    stage.status === 'completed'
      ? 'text-emerald-700'
      : stage.status === 'failed'
        ? 'text-red-700'
        : ['processing', 'created', 'retry'].includes(stage.status)
          ? 'text-blue-700'
          : 'text-slate-500';
  return (
    <td className={`px-3 py-3 text-xs ${color}`} title={stage.last_error ?? stage.message}>
      <p>{stage.message}</p>
      {suffix && <p className="mt-0.5 text-slate-400">{suffix}</p>}
    </td>
  );
}
