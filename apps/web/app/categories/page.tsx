'use client'

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';

type Category = {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  sort_order: number;
  is_default: boolean;
  parent_id: number | null;
  document_count: number;
};

export default function CategoriesPage() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [newCategory, setNewCategory] = useState({ slug: '', name: '', description: '' });
  const [submitting, setSubmitting] = useState(false);

  const loadCategories = useCallback(async () => {
    try {
      const res = await fetch('/api/categories', { cache: 'no-store' });
      if (!res.ok) throw new Error('暂时无法读取分类');
      setCategories(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取分类失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/categories', { cache: 'no-store', signal: controller.signal })
      .then(res => {
        if (!res.ok) throw new Error('暂时无法读取分类');
        return res.json();
      })
      .then((data: Category[]) => {
        setCategories(data);
        setError('');
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : '读取分类失败');
        setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!newCategory.slug.trim() || !newCategory.name.trim()) {
      setError('slug 和名称不能为空');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const res = await fetch('/api/categories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slug: newCategory.slug.trim().toLowerCase(),
          name: newCategory.name.trim(),
          description: newCategory.description.trim() || null,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || '创建分类失败');
      }
      setNewCategory({ slug: '', name: '', description: '' });
      await loadCategories();
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建分类失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (category: Category) => {
    if (category.document_count > 0) {
      setError('该分类仍有资料，无法删除');
      return;
    }
    if (!window.confirm(`确定删除分类 “${category.name}” 吗？`)) return;
    setError('');
    try {
      const res = await fetch(`/api/categories/${category.id}`, { method: 'DELETE' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || '删除失败');
      }
      await loadCategories();
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败');
    }
  };

  return (
    <main className="container mx-auto p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">← 返回资料列表</Link>

      <h1 className="mt-4 text-2xl font-bold">分类管理</h1>
      <p className="mt-2 text-sm text-slate-500">
        默认分类由系统初始化，删除有资料或子分类的分类会被拒绝。
      </p>

      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}

      <section className="mt-6 rounded-xl border border-slate-200 bg-white p-4">
        <h2 className="text-lg font-semibold">新增分类</h2>
        <form onSubmit={handleCreate} className="mt-3 grid gap-3 sm:grid-cols-2">
          <input
            type="text"
            placeholder="slug（英文小写）"
            value={newCategory.slug}
            onChange={e => setNewCategory(prev => ({ ...prev, slug: e.target.value }))}
            className="rounded border border-slate-300 px-3 py-2 text-sm font-mono"
            required
            disabled={submitting}
          />
          <input
            type="text"
            placeholder="分类名称"
            value={newCategory.name}
            onChange={e => setNewCategory(prev => ({ ...prev, name: e.target.value }))}
            className="rounded border border-slate-300 px-3 py-2 text-sm"
            required
            disabled={submitting}
          />
          <input
            type="text"
            placeholder="描述（可选）"
            value={newCategory.description}
            onChange={e => setNewCategory(prev => ({ ...prev, description: e.target.value }))}
            className="rounded border border-slate-300 px-3 py-2 text-sm sm:col-span-2"
            disabled={submitting}
          />
          <button
            type="submit"
            disabled={submitting}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50 sm:col-span-2"
          >
            {submitting ? '保存中…' : '保存分类'}
          </button>
        </form>
      </section>

      <section className="mt-6">
        {loading ? (
          <p className="text-slate-500">加载中…</p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-slate-500">
                <th className="py-2 pr-2">分类</th>
                <th className="py-2 pr-2">slug</th>
                <th className="py-2 pr-2">资料数</th>
                <th className="py-2 pr-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {categories.map(category => (
                <tr key={category.id} className="border-b border-slate-100">
                  <td className="py-2 pr-2 font-medium text-slate-800">
                    {category.name}
                    {category.is_default && (
                      <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">默认</span>
                    )}
                  </td>
                  <td className="py-2 pr-2 font-mono text-xs text-slate-500">{category.slug}</td>
                  <td className="py-2 pr-2 text-slate-500">{category.document_count}</td>
                  <td className="py-2 pr-2">
                    <button
                      type="button"
                      onClick={() => handleDelete(category)}
                      className="rounded border border-red-200 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={category.document_count > 0}
                      title={category.document_count > 0 ? '仍有资料，无法删除' : '删除分类'}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
