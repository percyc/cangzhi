'use client'

import { useState, useEffect } from 'react';
import Link from 'next/link';

type DocumentSummary = {
  summary: string;
  source: string;
};

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

type DocumentVersion = {
  id: number;
  version_number: number;
  content_hash: string;
  raw_content: string | null;
  processing_status: string;
  created_at: string;
};

type Document = {
  id: number;
  title: string;
  description: string | null;
  source_type: string;
  source_url: string | null;
  origin: {
    kind: string;
    label: string;
    connector_id: number | null;
    remote_path: string | null;
    connector_available: boolean;
  } | null;
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
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

const sourceTypeLabels: Record<string, string> = {
  note: '随手记',
  file: '文件',
  url: '链接',
};

export default function DocumentsListPage() {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    fetch('/api/documents')
      .then(res => {
        if (!res.ok) throw new Error('暂时无法读取资料');
        return res.json();
      })
      .then(data => {
        setDocuments(data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  return (
    <main className="container mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">资料库</h1>

      <div className="flex flex-wrap gap-3 mb-6">
        <Link
          href="/ask"
          className="px-4 py-2 bg-slate-900 text-white rounded hover:bg-slate-800"
        >
          问知识库
        </Link>
        <Link
          href="/search"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          搜索资料
        </Link>
        <Link
          href="/notes/new"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          记录一个想法
        </Link>
        <Link
          href="/files/upload"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          上传文件
        </Link>
        <Link
          href="/links/new"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          收藏链接
        </Link>
        <Link
          href="/categories"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          分类管理
        </Link>
      </div>

      {loading && <p className="text-slate-500">加载中…</p>}
      {error && <p className="text-red-600">错误：{error}</p>}

      {!loading && !error && (
        documents.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
            <p className="text-lg font-medium text-slate-800">还没有资料</p>
            <p className="mt-2 text-sm text-slate-500">从一条随手记、一篇文章链接或一个文件开始建立你的知识库。</p>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {documents.map(doc => {
              const version = doc.current_version;
              const status = version?.processing_status || 'created';
              return (
                <div
                  key={doc.id}
                  className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm hover:shadow transition-shadow"
                >
                  <h2 className="text-lg font-semibold text-slate-900">
                    <Link href={`/documents/${doc.id}`} className="hover:underline">
                      {doc.title}
                    </Link>
                  </h2>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <span className="rounded-full bg-slate-100 px-2 py-0.5">
                      {doc.origin?.kind === 'webdav'
                        ? `WebDAV · ${doc.origin.label}`
                        : sourceTypeLabels[doc.source_type] || doc.source_type}
                    </span>
                    <span>{statusLabels[status] || status}</span>
                    {doc.primary_category && (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800">
                        {doc.primary_category.name}
                      </span>
                    )}
                  </div>
                  {doc.origin?.remote_path && (
                    <p className="mt-2 truncate text-xs text-slate-400" title={doc.origin.remote_path}>
                      远端：{doc.origin.remote_path}
                    </p>
                  )}
                  {doc.summary?.summary && (
                    <p className="mt-3 text-sm text-slate-600 line-clamp-3">
                      {doc.summary.summary}
                    </p>
                  )}
                  {doc.tags.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-1">
                      {doc.tags.slice(0, 4).map(tag => (
                        <span key={tag.id} className="rounded-full border border-slate-200 px-2 py-0.5 text-xs text-slate-600">
                          #{tag.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )
      )}
    </main>
  );
}
