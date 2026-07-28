'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

type DocumentVersion = {
  id: number;
  version_number: number;
  raw_content: string | null;
  processing_status: string;
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
  source_type: string;
  created_at: string;
  current_version: DocumentVersion | null;
};

const statusLabels: Record<string, string> = {
  created: '待处理',
  processing: '处理中',
  retry: '等待重试',
  ready: '已完成',
  failed: '处理失败',
};

const statusColors: Record<string, string> = {
  created: 'text-amber-700',
  processing: 'text-blue-700',
  retry: 'text-amber-700',
  ready: 'text-green-700',
  failed: 'text-red-700',
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
    return <main className="container mx-auto p-4"><p>加载中...</p></main>;
  }
  if (error && !document) {
    return <main className="container mx-auto p-4"><p className="text-red-600">错误：{error}</p></main>;
  }
  if (!document) return null;

  const version = document.current_version;
  const status = version?.processing_status || 'created';
  const canRetry = status === 'failed';

  return (
    <main className="container mx-auto p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">← 返回资料列表</Link>

      <h1 className="mt-4 text-2xl font-bold">{document.title}</h1>
      <div className="mt-2 flex flex-wrap gap-4 text-sm text-gray-500">
        <span>类型：{document.source_type === 'note' ? '随手记' : '文件'}</span>
        <span>创建：{new Date(document.created_at).toLocaleString('zh-CN')}</span>
        <span className={statusColors[status] || 'text-gray-700'}>
          状态：{statusLabels[status] || status}
        </span>
      </div>

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
        <article className="mt-6 whitespace-pre-wrap rounded border bg-gray-50 p-4 leading-relaxed">
          {version.raw_content}
        </article>
      ) : (
        <div className="mt-6 rounded border bg-gray-50 p-4 text-gray-600">
          {status === 'failed' ? '暂时没有可显示的正文。' : '正在提取正文，请稍候…'}
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
