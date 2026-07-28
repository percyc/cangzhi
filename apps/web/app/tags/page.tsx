'use client'

import { useEffect, useState } from 'react';
import Link from 'next/link';

type Tag = {
  id: number;
  slug: string;
  name: string;
  description: string | null;
};

export default function TagsPage() {
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    fetch('/api/tags')
      .then(res => res.ok ? res.json() : Promise.reject(new Error('读取失败')))
      .then(setTags)
      .catch(err => setError(err instanceof Error ? err.message : '读取失败'))
      .finally(() => setLoading(false));
  }, []);

  return (
    <main className="container mx-auto p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">← 返回资料列表</Link>
      <h1 className="mt-4 text-2xl font-bold">标签</h1>
      <p className="mt-2 text-sm text-slate-500">由 AI 自动生成和整理中，可在资料详情中增删。</p>

      {loading && <p className="mt-4 text-slate-500">加载中…</p>}
      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}

      <div className="mt-6 flex flex-wrap gap-2">
        {tags.map(tag => (
          <span key={tag.id} className="rounded-full border border-slate-200 px-3 py-1 text-sm text-slate-700">
            #{tag.name}
          </span>
        ))}
        {!loading && tags.length === 0 && (
          <p className="text-sm text-slate-500">还没有标签。</p>
        )}
      </div>
    </main>
  );
}
