'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';

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

type RepairResult = {
  enqueued?: number;
  reset?: number;
};

export default function InboxPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [filter, setFilter] = useState<Filter>('attention');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [repairing, setRepairing] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch(
        '/api/documents/overview?limit=200&include_processing=true',
        { cache: 'no-store' },
      );
      if (!response.ok) throw new Error('资料列表读取失败');
      setDocuments((await response.json()) as DocumentItem[]);
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
    if (
      !documents.some(
        (document) => document.pipeline.overall_status === 'processing',
      )
    )
      return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load, documents]);

  const counts = useMemo(
    () => ({
      attention: documents.filter(
        (document) =>
          document.pipeline.overall_status !== 'completed' ||
          !document.pipeline.vector_searchable ||
          document.primary_category?.slug === 'inbox' ||
          Boolean(
            document.origin &&
              (!document.origin.connector_available ||
                ['failed', 'missing'].includes(
                  document.origin.source_status ?? '',
                )),
          ),
      ).length,
      all: documents.length,
      processing: documents.filter(
        (document) => document.pipeline.overall_status === 'processing',
      ).length,
      failed: documents.filter(
        (document) => document.pipeline.overall_status === 'failed',
      ).length,
      needs_organization: documents.filter(
        (document) => document.primary_category?.slug === 'inbox',
      ).length,
      source_issue: documents.filter(
        (document) =>
          Boolean(
            document.origin &&
              (!document.origin.connector_available ||
                ['failed', 'missing'].includes(
                  document.origin.source_status ?? '',
                )),
          ),
      ).length,
      not_vectorized: documents.filter(
        (document) => !document.pipeline.vector_searchable,
      ).length,
      completed: documents.filter(
        (document) => document.pipeline.overall_status === 'completed',
      ).length,
    }),
    [documents],
  );

  const visibleDocuments = documents.filter((document) => {
    if (filter === 'attention')
      return (
        document.pipeline.overall_status !== 'completed' ||
        !document.pipeline.vector_searchable ||
        document.primary_category?.slug === 'inbox' ||
        Boolean(
          document.origin &&
            (!document.origin.connector_available ||
              ['failed', 'missing'].includes(
                document.origin.source_status ?? '',
              )),
        )
      );
    if (filter === 'all') return true;
    if (filter === 'not_vectorized') return !document.pipeline.vector_searchable;
    if (filter === 'needs_organization')
      return document.primary_category?.slug === 'inbox';
    if (filter === 'source_issue')
      return Boolean(
        document.origin &&
          (!document.origin.connector_available ||
            ['failed', 'missing'].includes(
              document.origin.source_status ?? '',
            )),
      );
    return document.pipeline.overall_status === filter;
  });

  const parseStageFailedDocuments = useMemo(
    () =>
      documents.filter((document) => {
        const stages = document.pipeline.stages;
        return (
          stages.parsing.status === 'failed' ||
          stages.understanding.status === 'failed' ||
          stages.chunking.status === 'failed'
        );
      }),
    [documents],
  );

  const missingVectorDocuments = useMemo(
    () =>
      documents.filter(
        (document) =>
          document.pipeline.stages.embedding.status !== 'disabled' &&
          document.pipeline.stages.embedding.missing > 0,
      ),
    [documents],
  );

  const retryFailed = async () => {
    if (!parseStageFailedDocuments.length || retrying) return;
    setRetrying(true);
    try {
      await Promise.all(
        parseStageFailedDocuments.map(async (document) => {
          const response = await fetch(
            `/api/documents/${document.id}/reprocess`,
            { method: 'POST' },
          );
          if (!response.ok)
            throw new Error(`资料 ${document.id} 重试提交失败`);
        }),
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量重试失败');
    } finally {
      setRetrying(false);
    }
  };

  const repairMissingVectors = async () => {
    if (!missingVectorDocuments.length || repairing) return;
    setRepairing(true);
    setFeedback('');
    try {
      const response = await fetch('/api/documents/batch/repair-vectors', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          document_ids: missingVectorDocuments.map((document) => document.id),
        }),
      });
      if (!response.ok) throw new Error('补建缺失向量失败');
      const result = (await response.json()) as RepairResult;
      const enqueued = result.enqueued ?? 0;
      const reset = result.reset ?? 0;
      setFeedback(
        `已补建 ${enqueued} 项缺失向量并重置 ${reset} 项状态，正在重新处理…`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '补建缺失向量失败');
    } finally {
      setRepairing(false);
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

  return (
    <main className="container mx-auto max-w-6xl p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">收件箱</h1>
          <p className="mt-1 text-sm text-slate-500">
            只处理需要关注的资料：处理中、失败、待整理和尚未建立语义索引的内容。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={retryFailed}
            disabled={retrying || parseStageFailedDocuments.length === 0}
            className="rounded border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 disabled:opacity-40"
          >
            {retrying
              ? '正在提交…'
              : `重试解析失败（${parseStageFailedDocuments.length}）`}
          </button>
          <button
            type="button"
            onClick={repairMissingVectors}
            disabled={repairing || missingVectorDocuments.length === 0}
            className="rounded bg-violet-600 px-4 py-2 text-sm text-white disabled:opacity-40"
          >
            {repairing
              ? '正在补建…'
              : `补建缺失向量（${missingVectorDocuments.length}）`}
          </button>
        </div>
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
      {feedback && <p className="mt-4 text-sm text-violet-700">{feedback}</p>}
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
              {visibleDocuments.map((document) => {
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
          {visibleDocuments.length === 0 && (
            <p className="p-8 text-center text-sm text-slate-500">
              当前筛选下没有资料。
            </p>
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
