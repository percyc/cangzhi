'use client'

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { apiErrorMessage } from '@/lib/usability';

export default function NewNotePage() {
  const router = useRouter();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    if (!content.trim()) { setError('请先填写正文内容'); return; }
    setLoading(true);
    setError('');

    try {
      const res = await fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content, generate_title: !title.trim() }),
      });

      if (!res.ok) throw new Error(apiErrorMessage(await res.json().catch(() => null), '保存失败，请重试'));
      const data = await res.json();
      router.push(`/documents/${data.id}`);
    } catch (reason) {
      setError(reason instanceof TypeError ? '无法连接服务器。正文仍保留在此页，请检查网络后重试。' : reason instanceof Error ? reason.message : '保存失败，请重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>
      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">快速录入</p>
        <h1 className="mt-1 text-3xl font-semibold text-slate-950">记录一个想法</h1>
        <p className="mt-2 text-sm text-slate-500">先自由写下来，藏知会在后台完成整理、分类和索引。</p>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
        {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

        <div>
          <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-slate-700">
            标题 <span className="font-normal text-slate-400">可留空，自动取第一段</span>
          </label>
          <input
            id="title"
            type="text"
            maxLength={1024}
            disabled={loading}
            value={title}
            onChange={e => setTitle(e.target.value)}
            className="w-full rounded-xl border px-3.5 py-2.5"
            placeholder="给这条记录起个名字"
          />
        </div>

        <div>
          <label htmlFor="content" className="mb-1.5 block text-sm font-medium text-slate-700">
            内容 <span className="font-normal text-slate-400">支持 Markdown</span>
          </label>
          <textarea
            id="content"
            disabled={loading}
            value={content}
            onChange={e => setContent(e.target.value)}
            className="min-h-[360px] w-full resize-y rounded-xl border px-4 py-3 leading-7"
            placeholder="此刻你在想什么？"
            required
          />
        </div>

        <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-5">
          <button
            type="submit"
            disabled={loading || !content.trim()}
            className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {loading ? '保存中...' : '保存'}
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
