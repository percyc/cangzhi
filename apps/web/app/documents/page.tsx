'use client'

import { useState, useEffect } from 'react';
import Link from 'next/link';

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
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
  current_version: DocumentVersion | null;
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

  const getSourceTypeLabel = (type: string) => {
    switch (type) {
      case 'note': return '随手记';
      case 'file': return '文件';
      case 'url': return '链接';
      default: return type;
    }
  };

  const getStatusLabel = (doc: Document) => {
    if (!doc.current_version) return '';
    switch (doc.current_version.processing_status) {
      case 'created': return '待处理';
      case 'ready': return '已完成';
      case 'failed': return '处理失败';
      default: return doc.current_version.processing_status;
    }
  };

  return (
    <main className="container mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">文档列表</h1>

      <div className="flex gap-4 mb-6">
        <Link
          href="/notes/new"
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          新建随手记
        </Link>
        <Link
          href="/files/upload"
          className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700"
        >
          上传文件
        </Link>
      </div>

      {loading && <p>加载中...</p>}
      {error && <p className="text-red-600">错误: {error}</p>}

      {!loading && !error && (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {documents.map(doc => (
            <div
              key={doc.id}
              className="border rounded p-4 shadow-sm hover:shadow transition-shadow"
            >
              <h2 className="text-xl font-semibold mb-2">
                <Link href={`/documents/${doc.id}`} className="text-blue-600 hover:underline">
                  {doc.title}
                </Link>
              </h2>
              <div className="text-sm text-gray-500 space-y-1">
                <p>类型: {getSourceTypeLabel(doc.source_type)}</p>
                <p>创建时间: {new Date(doc.created_at).toLocaleString('zh-CN')}</p>
                {doc.current_version && (
                  <p>状态: {getStatusLabel(doc)}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
