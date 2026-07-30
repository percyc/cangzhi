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
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>
      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">网页收藏</p>
        <h1 className="mt-1 text-3xl font-semibold text-slate-950">保存一篇好文章</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">
          粘贴公开链接，藏知会自动提取正文、识别主题并建议合适的分类。
        </p>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
        {error && <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

        <div>
          <label htmlFor="url" className="mb-1.5 block text-sm font-medium text-slate-700">链接地址</label>
          <input
            id="url"
            type="url"
            value={url}
            onChange={e => setUrl(e.target.value)}
            className="w-full rounded-xl border px-3.5 py-3 font-mono text-sm"
            placeholder="https://example.com/article"
            required
            disabled={loading}
          />
          <p className="mt-2 text-xs leading-5 text-slate-400">
            出于安全考虑，不支持 localhost、内网地址或需要登录的链接。
          </p>
        </div>

        <div className="flex flex-wrap gap-3 border-t border-slate-100 pt-5">
          <button
            type="submit"
            disabled={loading}
            className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {loading ? '保存中...' : '保存链接'}
          </button>
          <Link
            href="/documents"
            className="rounded-xl border border-slate-300 px-5 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            取消
          </Link>
        </div>
      </form>
    </main>
  );
}
