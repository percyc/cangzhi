'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

type DocumentCategory = {
  id: number;
  slug: string;
  name: string;
};

type DocumentTag = {
  id: number;
  slug: string;
  name: string;
};

type DocumentSummary = {
  id: number;
  summary: string;
  source: string;
  confidence: number | null;
};

type DocumentVersion = {
  id: number;
  version_number: number;
  raw_content: string | null;
  processing_status: string;
  meta: {
    extraction_status?: string;
    extraction_message?: string;
  };
  structured_content: {
    metadata?: {
      author?: string | null;
      published_at?: string | null;
      canonical_url?: string | null;
    };
  } | null;
  blob: {
    id: number;
    original_filename: string | null;
    file_size: number;
    content_type: string;
  } | null;
};

type ProcessingJob = {
  id: number;
  status: string;
  last_error: string | null;
};

type Document = {
  id: number;
  title: string;
  description: string | null;
  source_type: string;
  source_url: string | null;
  created_at: string;
  current_version: DocumentVersion | null;
  primary_category: DocumentCategory | null;
  categories: DocumentCategory[];
  tags: DocumentTag[];
  summary: DocumentSummary | null;
};

const statusLabels: Record<string, string> = {
  created: '待处理',
  processing: '处理中',
  retry: '等待重试',
  ready: '已完成',
  failed: '处理失败',
  unsupported: '暂未提取正文',
};

const statusColors: Record<string, string> = {
  created: 'text-amber-700',
  processing: 'text-blue-700',
  retry: 'text-amber-700',
  ready: 'text-green-700',
  failed: 'text-red-700',
  unsupported: 'text-amber-700',
};

const sourceTypeLabels: Record<string, string> = {
  note: '随手记',
  file: '文件',
  url: '链接',
};

export default function DocumentDetailPage() {
  const params = useParams<{ id: string }>();
  const [document, setDocument] = useState<Document | null>(null);
  const [latestJob, setLatestJob] = useState<ProcessingJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retrying, setRetrying] = useState(false);

  const loadData = useCallback(async () => {
    try {
      const [documentResponse, jobResponse] = await Promise.all([
        fetch(`/api/documents/${params.id}`, { cache: 'no-store' }),
        fetch(`/api/documents/${params.id}/latest-job`, { cache: 'no-store' }),
      ]);
      if (!documentResponse.ok) {
        throw new Error(documentResponse.status === 404 ? '资料不存在' : '读取失败');
      }
      if (!jobResponse.ok) throw new Error('读取处理状态失败');
      setDocument(await documentResponse.json());
      setLatestJob(await jobResponse.json());
      setError('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }, [params.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadData(), 0);
    return () => window.clearTimeout(timer);
  }, [loadData]);

  useEffect(() => {
    const status = document?.current_version?.processing_status;
    if (!status || !['created', 'processing', 'retry'].includes(status)) return;
    const timer = window.setInterval(() => void loadData(), 3000);
    return () => window.clearInterval(timer);
  }, [document?.current_version?.processing_status, loadData]);

  const handleRetry = async () => {
    setRetrying(true);
    try {
      const response = await fetch(`/api/documents/${params.id}/reprocess`, {
        method: 'POST',
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || '提交重试失败');
      await loadData();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '提交重试失败');
    } finally {
      setRetrying(false);
    }
  };

  if (loading) {
    return <main className="container mx-auto p-4"><p>加载中…</p></main>;
  }
  if (error && !document) {
    return <main className="container mx-auto p-4"><p className="text-red-600">错误：{error}</p></main>;
  }
  if (!document) return null;

  const version = document.current_version;
  const status = version?.processing_status || 'created';
  const canRetry = ['failed', 'unsupported'].includes(status);
  const metadata = version?.structured_content?.metadata;

  return (
    <main className="container mx-auto p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">← 返回资料列表</Link>

      <h1 className="mt-4 text-2xl font-bold text-slate-900">{document.title}</h1>
      <div className="mt-2 flex flex-wrap gap-3 text-sm text-slate-500">
        <span>类型：{sourceTypeLabels[document.source_type] || document.source_type}</span>
        <span>创建：{new Date(document.created_at).toLocaleString('zh-CN')}</span>
        <span className={statusColors[status] || 'text-slate-700'}>
          状态：{statusLabels[status] || status}
        </span>
      </div>

      {document.source_url && (
        <div className="mt-3 text-sm">
          <span className="text-slate-500">来源：</span>
          <a
            href={document.source_url}
            target="_blank"
            rel="noreferrer noopener"
            className="font-mono text-blue-600 hover:underline break-all"
          >
            {document.source_url}
          </a>
        </div>
      )}

      {document.primary_category && (
        <div className="mt-3 text-sm text-slate-700">
          <span className="text-slate-500">主分类：</span>
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800">
            {document.primary_category.name}
          </span>
        </div>
      )}

      {document.tags.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-slate-500">标签：</span>
          {document.tags.map(tag => (
            <span key={tag.id} className="rounded-full border border-slate-200 px-2 py-0.5 text-slate-600">
              #{tag.name}
            </span>
          ))}
        </div>
      )}

      {document.summary?.summary && (
        <section className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-slate-500">摘要</h2>
          <p className="mt-2 text-sm leading-6 text-slate-700">{document.summary.summary}</p>
        </section>
      )}

      {(metadata?.author || metadata?.published_at) && (
        <div className="mt-3 text-xs text-slate-500">
          {metadata?.author && <span>作者：{metadata.author}</span>}
          {metadata?.author && metadata?.published_at && <span> · </span>}
          {metadata?.published_at && <span>发布日期：{metadata.published_at}</span>}
        </div>
      )}

      {document.source_type === 'note' && (
        <Link
          href={`/notes/${document.id}/edit`}
          className="mt-4 inline-block rounded border border-slate-300 px-3 py-2 text-sm"
        >
          编辑随手记
        </Link>
      )}

      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
      {latestJob?.last_error && status === 'failed' && (
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          处理失败：{latestJob.last_error}
        </div>
      )}

      {version?.raw_content ? (
        <article className="mt-6 whitespace-pre-wrap rounded-xl border border-slate-200 bg-slate-50 p-4 leading-relaxed text-slate-800">
          {version.raw_content}
        </article>
      ) : (
        <div className="mt-6 rounded-xl border border-slate-200 bg-slate-50 p-4 text-slate-600">
          {['created', 'processing', 'retry'].includes(status)
            ? '正在提取正文，请稍候…'
            : version?.meta?.extraction_message ||
              (status === 'ready'
                ? '处理已经完成，但没有识别到可显示的正文。你可以尝试重新处理。'
                : '暂时没有可显示的正文。')}
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-3">
        {document.source_type === 'file' && version?.blob && (
          <a
            href={`/api/files/blobs/${version.blob.id}/download`}
            className="rounded border border-blue-600 px-3 py-2 text-sm text-blue-600 hover:bg-blue-50"
          >
            下载原文件
          </a>
        )}
        {canRetry && (
          <button
            type="button"
            onClick={handleRetry}
            disabled={retrying}
            className="rounded bg-amber-600 px-3 py-2 text-sm text-white hover:bg-amber-700 disabled:opacity-50"
          >
            {retrying ? '正在提交…' : '重新处理'}
          </button>
        )}
      </div>
    </main>
  );
}
