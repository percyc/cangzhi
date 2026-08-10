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

type PipelineSummary = {
  overall_status: 'processing' | 'completed' | 'failed';
  keyword_searchable: boolean;
  vector_searchable: boolean;
  stages: {
    embedding: { status: string; completed: number; total: number; missing: number };
  };
};

type Document = {
  id: number;
  title: string;
  description: string | null;
  source_type: string;
  source_url: string | null;
  content_kind: 'document' | 'dataset' | 'note';
  origin: {
    kind: string;
    label: string;
    connector_id: number | null;
    remote_path: string | null;
    connector_available: boolean;
  } | null;
  is_deleted: boolean;
  deleted_at: string | null;
  delete_reason: string | null;
  created_at: string;
  updated_at: string;
  current_version: DocumentVersion | null;
  primary_category: DocumentCategory | null;
  categories: DocumentCategory[];
  tags: DocumentTag[];
  summary: DocumentSummary | null;
  pipeline: PipelineSummary | null;
};

const statusLabels: Record<string, string> = {
  created: '待处理',
  processing: '处理中',
  retry: '等待重试',
  ready: '已完成',
  failed: '处理失败',
  unsupported: '暂未提取正文',
  completed: '已完成',
};

const sourceTypeLabels: Record<string, string> = {
  note: '随手记',
  file: '文件',
  url: '链接',
};

const contentKindLabels: Record<string, string> = {
  document: '文档',
  dataset: '数据集',
  note: '随手记',
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
    return fetch(
      `/api/documents/overview?limit=200&deleted=${view === 'trash'}&include_processing=${view === 'active'}`,
    )
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

  const permanentlyDeleteSelected = async () => {
    if (
      !selected.length ||
      !window.confirm(
        `永久删除选中的 ${selected.length} 条资料？正文、切片和向量都会删除，且无法恢复。`,
      )
    ) return;
    setActing(true);
    try {
      const response = await fetch('/api/documents/batch/permanent-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ document_ids: selected }),
      });
      if (!response.ok) throw new Error('永久删除失败');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '永久删除失败');
    } finally {
      setActing(false);
    }
  };

  const emptyTrash = async () => {
    if (
      !documents.length ||
      !window.confirm(
        `清空回收站中的 ${documents.length} 条资料？此操作无法恢复，WebDAV 远端文件不会被删除。`,
      )
    ) return;
    setActing(true);
    try {
      const response = await fetch('/api/documents/trash/empty', {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error('清空回收站失败');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '清空回收站失败');
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

  const permanentlyDeleteSingle = async (document: Document) => {
    if (
      !window.confirm(
        `永久删除“${document.title}”？正文、切片和向量都会删除，且无法恢复。`,
      )
    ) return;
    setActing(true);
    try {
      const response = await fetch(`/api/documents/${document.id}/permanent`, {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error('永久删除失败');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '永久删除失败');
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
    <main className="mx-auto max-w-7xl px-4 sm:px-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">Personal Library</p>
          <h1 className="mt-1 text-3xl font-semibold text-slate-950">知识库</h1>
          <p className="mt-1 text-sm text-slate-500">让收藏、文档和想法在这里持续沉淀。</p>
        </div>
        <div className="flex items-center gap-2">
          {view === 'trash' && documents.length > 0 && (
            <button type="button" disabled={acting} onClick={() => void emptyTrash()} className="rounded-xl px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40">
              清空回收站
            </button>
          )}
          <div className="flex rounded-xl border border-slate-300 bg-white p-1 text-sm shadow-sm">
            <button type="button" onClick={() => setView('active')} className={`rounded-lg px-3 py-1.5 ${view === 'active' ? 'bg-slate-900 text-white' : 'text-slate-600'}`}>全部资料</button>
            <button type="button" onClick={() => setView('trash')} className={`rounded-lg px-3 py-1.5 ${view === 'trash' ? 'bg-slate-900 text-white' : 'text-slate-600'}`}>回收站</button>
          </div>
        </div>
      </div>

      {view === 'active' && <div className="mb-6 flex flex-wrap gap-2">
        <Link
          href="/categories"
          className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm text-slate-700 hover:border-slate-400 hover:bg-slate-50"
        >
          分类管理
        </Link>
        <Link
          href="/tags"
          className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm text-slate-700 hover:border-slate-400 hover:bg-slate-50"
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
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3 text-sm">
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
              {view === 'trash' && (
                <button type="button" disabled={!selected.length || acting} onClick={() => void permanentlyDeleteSelected()} className="rounded border border-red-300 px-3 py-1.5 text-red-700 disabled:opacity-40">
                  永久删除选中资料
                </button>
              )}
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
              const status = doc.pipeline?.overall_status || version?.processing_status || 'created';
              return (
                <div
                  key={doc.id}
                  className="group min-w-0 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition-all hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-lg hover:shadow-slate-950/5"
                >
                  {manageMode && <label className="mb-2 flex items-center gap-2 text-xs text-slate-500">
                    <input type="checkbox" checked={selected.includes(doc.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, doc.id] : current.filter((id) => id !== doc.id))} />
                    选择
                  </label>}
                  <div className="flex items-start justify-between gap-2">
                    <h2 className="min-w-0 break-words text-lg font-semibold leading-6 text-slate-900">
                      {view === 'active' ? (
                        <Link href={`/documents/${doc.id}`} className="decoration-slate-300 underline-offset-4 hover:underline">
                          {doc.title}
                        </Link>
                      ) : doc.title}
                    </h2>
                    {!manageMode && (
                      <details className="relative">
                        <summary className="cursor-pointer list-none rounded px-2 py-1 text-slate-500 hover:bg-slate-100">•••</summary>
                        <div className="absolute right-0 z-10 mt-1 w-32 rounded-lg border border-slate-200 bg-white p-1 text-sm shadow-lg">
                          {view === 'active' && <Link href={`/documents/${doc.id}`} className="block rounded px-2 py-1.5 hover:bg-slate-100">查看与编辑</Link>}
                          <button type="button" disabled={acting} onClick={() => void updateSingleState(doc)} className={`block w-full rounded px-2 py-1.5 text-left hover:bg-slate-100 ${view === 'trash' ? 'text-emerald-700' : 'text-red-700'}`}>
                            {view === 'trash' ? '恢复资料' : '移入回收站'}
                          </button>
                          {view === 'trash' && (
                            <button type="button" disabled={acting} onClick={() => void permanentlyDeleteSingle(doc)} className="block w-full rounded px-2 py-1.5 text-left text-red-700 hover:bg-red-50">
                              永久删除
                            </button>
                          )}
                        </div>
                      </details>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <span className={`rounded-full px-2 py-0.5 ${doc.content_kind === 'dataset' ? 'bg-cyan-100 text-cyan-800' : 'bg-slate-100 text-slate-600'}`}>
                      {contentKindLabels[doc.content_kind] || '文档'}
                    </span>
                    <span className="rounded-full bg-slate-100 px-2 py-0.5">
                      {doc.origin?.kind === 'webdav'
                        ? `WebDAV · ${doc.origin.label}`
                        : sourceTypeLabels[doc.source_type] || doc.source_type}
                    </span>
                    <span>{statusLabels[status] || status}</span>
                    {view === 'active' && doc.pipeline && (
                      <>
                        <span className={doc.pipeline.keyword_searchable ? 'text-emerald-700' : 'text-slate-400'}>
                          {doc.pipeline.keyword_searchable ? '正文可检索' : '正文未就绪'}
                        </span>
                        <span className={doc.pipeline.vector_searchable ? 'text-violet-700' : 'text-slate-400'}>
                          {doc.pipeline.vector_searchable
                            ? '语义可检索'
                            : doc.pipeline.stages.embedding.status === 'disabled'
                              ? '未启用向量'
                              : `缺 ${doc.pipeline.stages.embedding.missing} 个向量`}
                        </span>
                      </>
                    )}
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
                  {view === 'trash' && doc.deleted_at && (
                    <p className="mt-2 text-xs text-slate-400">
                      删除于 {new Date(doc.deleted_at).toLocaleString('zh-CN')}
                    </p>
                  )}
                  {doc.summary?.summary && (
                    <p className="mt-3 line-clamp-3 text-sm leading-6 text-slate-600">
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
