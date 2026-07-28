'use client'

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

export default function NewLinkPage() {
  const router = useRouter();
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) {
      setError('请粘贴一个链接');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const res = await fetch('/api/sources/url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim() }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || '提交失败，请检查链接后重试');
        setLoading(false);
        return;
      }
      const data = await res.json();
      router.push(`/documents/${data.id}`);
    } catch {
      setError('网络错误，请重试');
      setLoading(false);
    }
  };

  return (
    <main className="container mx-auto p-4">
      <div className="mb-4">
        <Link href="/documents" className="text-blue-600 hover:underline">← 返回资料列表</Link>
      </div>

      <h1 className="text-2xl font-bold mb-2">收藏链接</h1>
      <p className="mb-6 text-sm text-slate-500">
        粘贴一个公开的 http 或 https 链接，藏知会自动抓取正文、识别主题并放到合适的分类下。
      </p>

      <form onSubmit={handleSubmit} className="max-w-2xl space-y-4">
        {error && <div className="p-3 bg-red-50 text-red-700 rounded text-sm">{error}</div>}

        <div>
          <label htmlFor="url" className="block text-sm font-medium mb-1">链接地址</label>
          <input
            id="url"
            type="url"
            value={url}
            onChange={e => setUrl(e.target.value)}
            className="w-full px-3 py-2 border rounded font-mono text-sm"
            placeholder="https://example.com/article"
            required
            disabled={loading}
          />
          <p className="mt-1 text-xs text-slate-500">
            出于安全考虑，不支持 localhost、内网地址或需要登录的链接。
          </p>
        </div>

        <div className="flex gap-4">
          <button
            type="submit"
            disabled={loading}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? '保存中...' : '保存链接'}
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
