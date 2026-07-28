'use client'

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

export default function NewNotePage() {
  const router = useRouter();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    const res = await fetch('/api/notes', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        title,
        content,
        generate_title: !title.trim(),
      }),
    });

    if (!res.ok) {
      setError('保存失败，请重试');
      setLoading(false);
      return;
    }

    const data = await res.json();
    router.push(`/documents/${data.id}`);
  };

  return (
    <main className="container mx-auto p-4">
      <div className="mb-4">
        <Link href="/documents" className="text-blue-600 hover:underline">← 返回文档列表</Link>
      </div>

      <h1 className="text-2xl font-bold mb-6">新建随手记</h1>

      <form onSubmit={handleSubmit} className="max-w-3xl space-y-4">
        {error && <div className="p-3 bg-red-50 text-red-600 rounded">{error}</div>}

        <div>
          <label htmlFor="title" className="block text-sm font-medium mb-1">
            标题 （留空会自动取第一段）
          </label>
          <input
            id="title"
            type="text"
            value={title}
            onChange={e => setTitle(e.target.value)}
            className="w-full px-3 py-2 border rounded"
            placeholder="请输入标题"
          />
        </div>

        <div>
          <label htmlFor="content" className="block text-sm font-medium mb-1">
            内容 （Markdown 格式）
          </label>
          <textarea
            id="content"
            value={content}
            onChange={e => setContent(e.target.value)}
            className="w-full px-3 py-2 border rounded min-h-[300px]"
            placeholder="开始记录想法..."
            required
          />
        </div>

        <div className="flex gap-4">
          <button
            type="submit"
            disabled={loading}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? '保存中...' : '保存'}
          </button>
          <Link
            href="/documents"
            className="px-4 py-2 border rounded hover:bg-gray-50"
          >
            取消
          </Link>
        </div>
      </form>
    </main>
  );
}
