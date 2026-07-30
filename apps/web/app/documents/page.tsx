'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

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
type OrganizeOption = { id: number; name: string };

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
  const [view, setView] = useState<'active' | 'trash'>('active');
  const [manageMode, setManageMode] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [categoryOptions, setCategoryOptions] = useState<OrganizeOption[]>([]);
  const [tagOptions, setTagOptions] = useState<OrganizeOption[]>([]);
  const [batchCategoryId, setBatchCategoryId] = useState('');
  const [batchTagId, setBatchTagId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [acting, setActing] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    return fetch(`/api/documents?limit=200&deleted=${view === 'trash'}`)
      .then(res => {
        if (!res.ok) throw new Error('暂时无法读取资料');
        return res.json();
      })
      .then(data => {
        setDocuments(data);
        setSelected([]);
        setError('');
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, [view]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch('/api/categories', { signal: controller.signal }).then((response) =>
        response.ok ? response.json() : Promise.reject(new Error('分类读取失败')),
      ),
      fetch('/api/tags', { signal: controller.signal }).then((response) =>
        response.ok ? response.json() : Promise.reject(new Error('标签读取失败')),
      ),
    ])
      .then(([categories, tags]) => {
        setCategoryOptions(categories as OrganizeOption[]);
        setTagOptions(tags as OrganizeOption[]);
      })
      .catch((caught) => {
        if (!controller.signal.aborted)
          setError(caught instanceof Error ? caught.message : '整理选项读取失败');
      });
    return () => controller.abort();
  }, []);

  const runBatchAction = async () => {
    if (!selected.length) return;
    const action = view === 'trash' ? 'restore' : 'trash';
    if (
      action === 'trash' &&
      !window.confirm(`确定将选中的 ${selected.length} 条资料移入回收站吗？`)
    ) return;
    setActing(true);
    try {
      const response = await fetch(`/api/documents/batch/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ document_ids: selected }),
      });
      if (!response.ok) throw new Error(action === 'trash' ? '删除失败' : '恢复失败');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '操作失败');
    } finally {
      setActing(false);
    }
  };

  const organizeSelected = async () => {
    if (!selected.length || (!batchCategoryId && !batchTagId)) return;
    setActing(true);
    try {
      const response = await fetch('/api/documents/batch/organize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          document_ids: selected,
          category_id: batchCategoryId ? Number(batchCategoryId) : null,
          add_tag_ids: batchTagId ? [Number(batchTagId)] : [],
        }),
      });
      if (!response.ok) throw new Error('批量整理失败');
      setBatchCategoryId('');
      setBatchTagId('');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量整理失败');
    } finally {
      setActing(false);
    }
  };

  const updateSingleState = async (document: Document) => {
    const restoring = view === 'trash';
    if (
      !restoring &&
      !window.confirm(`确定将“${document.title}”移入回收站吗？`)
    ) return;
    setActing(true);
    try {
      const response = await fetch(
        restoring
          ? `/api/documents/${document.id}/restore`
          : `/api/documents/${document.id}`,
        { method: restoring ? 'POST' : 'DELETE' },
      );
      if (!response.ok) throw new Error(restoring ? '恢复失败' : '删除失败');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '操作失败');
    } finally {
      setActing(false);
    }
  };

  const categories = Array.from(
    new Map(
      documents.flatMap((document) => document.categories).map((item) => [item.id, item]),
    ).values(),
  );
  const visibleDocuments =
    categoryId === null
      ? documents
      : documents.filter((document) =>
          document.categories.some((category) => category.id === categoryId),
        );

  return (
    <main className="container mx-auto p-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">知识库</h1>
          <p className="mt-1 text-sm text-slate-500">选择资料可批量管理；删除后先进入回收站。</p>
        </div>
        <div className="flex rounded-lg border border-slate-300 bg-white p-1 text-sm">
          <button type="button" onClick={() => setView('active')} className={`rounded px-3 py-1.5 ${view === 'active' ? 'bg-slate-900 text-white' : 'text-slate-600'}`}>全部资料</button>
          <button type="button" onClick={() => setView('trash')} className={`rounded px-3 py-1.5 ${view === 'trash' ? 'bg-slate-900 text-white' : 'text-slate-600'}`}>回收站</button>
        </div>
      </div>

      {view === 'active' && <div className="flex flex-wrap gap-3 mb-6">
        <Link
          href="/categories"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          分类管理
        </Link>
        <Link
          href="/tags"
          className="px-4 py-2 border border-slate-300 rounded hover:bg-slate-50"
        >
          标签管理
        </Link>
      </div>}

      {view === 'active' && categories.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-2">
          <button type="button" onClick={() => setCategoryId(null)} className={`rounded-full px-3 py-1.5 text-sm ${categoryId === null ? 'bg-slate-900 text-white' : 'border border-slate-300 bg-white text-slate-600'}`}>全部分类</button>
          {categories.map((category) => (
            <button key={category.id} type="button" onClick={() => setCategoryId(category.id)} className={`rounded-full px-3 py-1.5 text-sm ${categoryId === category.id ? 'bg-slate-900 text-white' : 'border border-slate-300 bg-white text-slate-600'}`}>{category.name}</button>
          ))}
        </div>
      )}

      {visibleDocuments.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white p-3 text-sm">
          {!manageMode ? (
            <button type="button" onClick={() => setManageMode(true)} className="rounded border border-slate-300 px-3 py-1.5 text-slate-700 hover:bg-slate-50">
              批量管理
            </button>
          ) : (
            <>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={selected.length === visibleDocuments.length} onChange={(event) => setSelected(event.target.checked ? visibleDocuments.map((item) => item.id) : [])} />
                全选当前结果
              </label>
              <span className="text-slate-500">已选 {selected.length} 条</span>
              {view === 'active' && (
                <>
                  <select value={batchCategoryId} onChange={(event) => setBatchCategoryId(event.target.value)} className="rounded border border-slate-300 px-2 py-1.5">
                    <option value="">主分类不变</option>
                    {categoryOptions.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
                  </select>
                  <select value={batchTagId} onChange={(event) => setBatchTagId(event.target.value)} className="max-w-48 rounded border border-slate-300 px-2 py-1.5">
                    <option value="">不添加标签</option>
                    {tagOptions.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
                  </select>
                  <button type="button" disabled={!selected.length || (!batchCategoryId && !batchTagId) || acting} onClick={organizeSelected} className="rounded bg-blue-700 px-3 py-1.5 text-white disabled:opacity-40">
                    应用整理
                  </button>
                </>
              )}
              <button type="button" disabled={!selected.length || acting} onClick={runBatchAction} className={`rounded px-3 py-1.5 text-white disabled:opacity-40 ${view === 'trash' ? 'bg-emerald-700' : 'bg-red-700'}`}>
                {acting ? '处理中…' : view === 'trash' ? '恢复选中资料' : '移入回收站'}
              </button>
              <button type="button" onClick={() => { setManageMode(false); setSelected([]); }} className="rounded px-3 py-1.5 text-slate-600 hover:bg-slate-100">完成</button>
            </>
          )}
        </div>
      )}

      {loading && <p className="text-slate-500">加载中…</p>}
      {error && <p className="text-red-600">错误：{error}</p>}

      {!loading && !error && (
        visibleDocuments.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
            <p className="text-lg font-medium text-slate-800">{view === 'trash' ? '回收站为空' : '还没有资料'}</p>
            <p className="mt-2 text-sm text-slate-500">{view === 'trash' ? '删除的资料会暂存在这里，可以随时恢复。' : '从一条随手记、一篇文章链接或一个文件开始建立你的知识库。'}</p>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {visibleDocuments.map(doc => {
              const version = doc.current_version;
              const status = version?.processing_status || 'created';
              return (
                <div
                  key={doc.id}
                  className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm hover:shadow transition-shadow"
                >
                  {manageMode && <label className="mb-2 flex items-center gap-2 text-xs text-slate-500">
                    <input type="checkbox" checked={selected.includes(doc.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, doc.id] : current.filter((id) => id !== doc.id))} />
                    选择
                  </label>}
                  <div className="flex items-start justify-between gap-2">
                    <h2 className="text-lg font-semibold text-slate-900">
                      <Link href={`/documents/${doc.id}`} className="hover:underline">
                        {doc.title}
                      </Link>
                    </h2>
                    {!manageMode && (
                      <details className="relative">
                        <summary className="cursor-pointer list-none rounded px-2 py-1 text-slate-500 hover:bg-slate-100">•••</summary>
                        <div className="absolute right-0 z-10 mt-1 w-32 rounded-lg border border-slate-200 bg-white p-1 text-sm shadow-lg">
                          {view === 'active' && <Link href={`/documents/${doc.id}`} className="block rounded px-2 py-1.5 hover:bg-slate-100">查看与编辑</Link>}
                          <button type="button" disabled={acting} onClick={() => void updateSingleState(doc)} className={`block w-full rounded px-2 py-1.5 text-left hover:bg-slate-100 ${view === 'trash' ? 'text-emerald-700' : 'text-red-700'}`}>
                            {view === 'trash' ? '恢复资料' : '移入回收站'}
                          </button>
                        </div>
                      </details>
                    )}
                  </div>
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
