'use client'

import { useState, useEffect } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';

type DocumentVersion = {
  id: number;
  version_number: number;
  content_hash: string;
  raw_content: string | null;
  processing_status: string;
  created_at: string;
  blob: {
    id: number;
    original_filename: string | null;
    file_size: number;
    content_type: string;
  } | null;
};

type Document = {
  id: number;
  title: string;
  description: string | null;
  source_type: string;
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
  current_version: DocumentVersion | null;
};

export default function DocumentDetailPage() {
  const params = useParams<{ id: string }>();
  const [document, setDocument] = useState<Document | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    fetch(`/api/documents/${params.id}`)
      .then(res => {
        if (!res.ok) throw new Error(res.status === 404 ? '资料不存在' : '读取失败');
        return res.json();
      })
      .then(data => {
        setDocument(data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, [params.id]);

  if (loading) {
    return <main className="container mx-auto p-4"><p>加载中...</p></main>;
  }

  if (error) {
    return <main className="container mx-auto p-4"><p className="text-red-600">错误: {error}</p></main>;
  }

  if (!document) return null;

  const getSourceTypeLabel = (type: string) => {
    switch (type) {
      case 'note': return '随手记';
      case 'file': return '文件';
      case 'url': return '链接';
      default: return type;
    }
  };

  const getStatusLabel = (status: string) => {
    switch (status) {
      case 'created': return '待处理';
      case 'ready': return '已完成';
      case 'failed': return '处理失败';
      default: return status;
    }
  };

  return (
    <main className="container mx-auto p-4">
      <div className="mb-4">
        <Link href="/documents" className="text-blue-600 hover:underline">← 返回文档列表</Link>
      </div>

      <h1 className="text-2xl font-bold mb-2">{document.title}</h1>
      {document.source_type === 'note' && (
        <Link
          href={`/notes/${document.id}/edit`}
          className="mb-5 inline-block rounded border border-slate-300 px-3 py-2 text-sm"
        >
          编辑随手记
        </Link>
      )}
      <div className="text-sm text-gray-500 space-x-4 mb-6">
        <span>类型: {getSourceTypeLabel(document.source_type)}</span>
        <span>创建: {new Date(document.created_at).toLocaleString('zh-CN')}</span>
        {document.current_version && (
          <span>状态: {getStatusLabel(document.current_version.processing_status)}</span>
        )}
      </div>

      {document.current_version?.raw_content && (
        <div className="border rounded p-4 bg-gray-50 whitespace-pre-wrap leading-relaxed">
          {document.current_version.raw_content}
        </div>
      )}

      {document.source_type === 'file' && (
        <div className="mt-4 p-4 border rounded bg-yellow-50">
          <p className="text-yellow-800">文件已安全保存，正文解析将在后续版本完成。</p>
          {document.current_version?.blob && (
            <a
              className="mt-3 inline-block text-blue-700 underline"
              href={`/api/files/blobs/${document.current_version.blob.id}/download`}
            >
              下载原文件
            </a>
          )}
        </div>
      )}
    </main>
  );
}
