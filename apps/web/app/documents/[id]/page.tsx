'use client';

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

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

type DocumentSummary = {
  id: number;
  summary: string;
  source: string;
  confidence: number | null;
};

type DocumentVersion = {
  id: number;
  version_number: number;
  raw_content: string | null;
  processing_status: string;
  meta: {
    extraction_status?: string;
    extraction_message?: string;
  };
  structured_content: {
    document_type?: string;
    metadata?: {
      author?: string | null;
      published_at?: string | null;
      canonical_url?: string | null;
    };
  } | null;
  blob: {
    id: number;
    original_filename: string | null;
    file_size: number;
    content_type: string;
  } | null;
};

type ProcessingJob = {
  id: number;
  status: string;
  last_error: string | null;
};

type PipelineStage = {
  status: string;
  message: string;
  last_error?: string | null;
};

type ProcessingPipeline = {
  overall_status: 'processing' | 'completed' | 'failed';
  keyword_searchable: boolean;
  vector_searchable: boolean;
  stages: {
    parsing: PipelineStage;
    understanding: PipelineStage;
    chunking: PipelineStage & { child_chunks: number };
    embedding: PipelineStage & {
      profile_id: number | null;
      model: string | null;
      completed: number;
      total: number;
      failed: number;
    };
  };
};

type CategoryOption = {
  id: number;
  slug: string;
  name: string;
};
type TagOption = {
  id: number;
  slug: string;
  name: string;
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
  created_at: string;
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

const statusColors: Record<string, string> = {
  created: 'text-amber-700',
  processing: 'text-blue-700',
  retry: 'text-amber-700',
  ready: 'text-green-700',
  failed: 'text-red-700',
  unsupported: 'text-amber-700',
};

const sourceTypeLabels: Record<string, string> = {
  note: '随手记',
  file: '文件',
  url: '链接',
};

export default function DocumentDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [document, setDocument] = useState<Document | null>(null);
  const [latestJob, setLatestJob] = useState<ProcessingJob | null>(null);
  const [pipeline, setPipeline] = useState<ProcessingPipeline | null>(null);
  const [categories, setCategories] = useState<CategoryOption[]>([]);
  const [tagOptions, setTagOptions] = useState<TagOption[]>([]);
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | ''>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [savingCategory, setSavingCategory] = useState(false);
  const [categoryMessage, setCategoryMessage] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [editingMetadata, setEditingMetadata] = useState(false);
  const [savingMetadata, setSavingMetadata] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftSummary, setDraftSummary] = useState('');
  const [draftTagIds, setDraftTagIds] = useState<number[]>([]);
  const [pipelineExpanded, setPipelineExpanded] = useState(false);

  const handleDelete = async () => {
    if (!window.confirm('确定删除这条资料吗？资料将进入回收站，可以恢复。')) return;
    setDeleting(true);
    try {
      const response = await fetch(`/api/documents/${params.id}`, {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error('删除资料失败');
      router.push('/documents');
      router.refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除资料失败');
      setDeleting(false);
    }
  };

  const loadData = useCallback(async () => {
    try {
      const [documentResponse, jobResponse, pipelineResponse, categoriesResponse, tagsResponse] =
        await Promise.all([
          fetch(`/api/documents/${params.id}`, { cache: 'no-store' }),
          fetch(`/api/documents/${params.id}/latest-job`, { cache: 'no-store' }),
          fetch(`/api/documents/${params.id}/processing-status`, {
            cache: 'no-store',
          }),
          fetch(`/api/categories`, { cache: 'no-store' }),
          fetch(`/api/tags`, { cache: 'no-store' }),
        ]);
      if (!documentResponse.ok) {
        throw new Error(documentResponse.status === 404 ? '资料不存在' : '读取失败');
      }
      if (!jobResponse.ok) throw new Error('读取处理状态失败');
      if (!pipelineResponse.ok) throw new Error('读取处理进度失败');
      if (!categoriesResponse.ok) throw new Error('读取分类失败');
      if (!tagsResponse.ok) throw new Error('读取标签失败');
      const documentBody = (await documentResponse.json()) as Document;
      setDocument(documentBody);
      setLatestJob(await jobResponse.json());
      setPipeline((await pipelineResponse.json()) as ProcessingPipeline);
      const categoryBody = (await categoriesResponse.json()) as CategoryOption[];
      setCategories(categoryBody);
      setTagOptions((await tagsResponse.json()) as TagOption[]);
      setSelectedCategoryId(documentBody.primary_category?.id ?? '');
      setDraftTitle(documentBody.title);
      setDraftSummary(documentBody.summary?.summary ?? '');
      setDraftTagIds(documentBody.tags.map((tag) => tag.id));
      setError('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }, [params.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadData(), 0);
    return () => window.clearTimeout(timer);
  }, [loadData]);

  useEffect(() => {
    if (pipeline?.overall_status !== 'processing') return;
    const timer = window.setInterval(() => void loadData(), 3000);
    return () => window.clearInterval(timer);
  }, [pipeline?.overall_status, loadData]);

  const handleRetry = async () => {
    setRetrying(true);
    try {
      const response = await fetch(`/api/documents/${params.id}/reprocess`, {
        method: 'POST',
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || '提交重试失败');
      await loadData();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '提交重试失败');
    } finally {
      setRetrying(false);
    }
  };

  const handleSaveCategory = async () => {
    if (selectedCategoryId === '') return;
    setSavingCategory(true);
    setCategoryMessage('');
    try {
      const response = await fetch(
        `/api/documents/${params.id}/category`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category_id: selectedCategoryId }),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || '保存分类失败');
      }
      const updated = (await response.json()) as Document;
      setDocument(updated);
      setSelectedCategoryId(updated.primary_category?.id ?? '');
      setCategoryMessage('主分类已更新');
    } catch (caught) {
      setCategoryMessage(
        caught instanceof Error ? caught.message : '保存分类失败',
      );
    } finally {
      setSavingCategory(false);
    }
  };

  const handleSaveMetadata = async () => {
    if (!draftTitle.trim()) return;
    setSavingMetadata(true);
    setError('');
    try {
      const metadataResponse = await fetch(
        `/api/documents/${params.id}/metadata`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: draftTitle.trim(),
            summary: draftSummary,
          }),
        },
      );
      if (!metadataResponse.ok) throw new Error('标题或摘要保存失败');
      const tagsResponse = await fetch(`/api/documents/${params.id}/tags`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag_ids: draftTagIds }),
      });
      if (!tagsResponse.ok) throw new Error('标签保存失败');
      setDocument((await tagsResponse.json()) as Document);
      setEditingMetadata(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '整理信息保存失败');
    } finally {
      setSavingMetadata(false);
    }
  };

  if (loading) {
    return <main className="container mx-auto p-4"><p>加载中…</p></main>;
  }
  if (error && !document) {
    return <main className="container mx-auto p-4"><p className="text-red-600">错误：{error}</p></main>;
  }
  if (!document) return null;

  const version = document.current_version;
  const status = version?.processing_status || 'created';
  const canRetry = ['failed', 'unsupported'].includes(status);
  const metadata = version?.structured_content?.metadata;
  const rendersAsMarkdown =
    document.source_type === 'note' ||
    version?.structured_content?.document_type === 'markdown' ||
    Boolean(version?.blob?.original_filename?.toLowerCase().endsWith('.md'));
  const canDownload =
    document.source_type === 'file' &&
    Boolean(
      version?.blob ||
        (document.origin?.kind === 'webdav' &&
          document.origin.connector_available),
    );
  const knowledgeStatus =
    pipeline?.overall_status === 'completed'
      ? '知识库已就绪'
      : pipeline?.overall_status === 'failed'
        ? '知识库部分失败'
        : '知识库处理中';
  const informationSummary = [
    document.primary_category?.name || '未分类',
    document.tags.length ? `${document.tags.length} 个标签` : '无标签',
    document.origin?.kind === 'webdav'
      ? `WebDAV · ${document.origin.label}`
      : sourceTypeLabels[document.source_type] || document.source_type,
  ].join(' · ');

  return (
    <main className="mx-auto max-w-6xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>

      <div className="mt-4">
        <h1 className="max-w-5xl text-2xl font-semibold text-slate-950 sm:text-3xl">{document.title}</h1>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <span className={`rounded-full bg-slate-100 px-2.5 py-1 ${statusColors[status] || 'text-slate-700'}`}>
          正文{statusLabels[status] || status}
        </span>
        {pipeline && (
          <span
            className={`rounded-full px-2.5 py-1 ${
              pipeline.overall_status === 'completed'
                ? 'bg-emerald-100 text-emerald-800'
                : pipeline.overall_status === 'failed'
                  ? 'bg-red-100 text-red-800'
                  : 'bg-blue-100 text-blue-800'
            }`}
          >
            {knowledgeStatus}
          </span>
        )}
        <span className="text-slate-400">
          {sourceTypeLabels[document.source_type] || document.source_type} ·{' '}
          {new Date(document.created_at).toLocaleDateString('zh-CN')}
        </span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
        <button type="button" onClick={() => setEditingMetadata((value) => !value)} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
          {editingMetadata ? '取消编辑' : '编辑信息'}
        </button>
        {document.source_type === 'note' && (
          <Link href={`/notes/${document.id}/edit`} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
            编辑正文
          </Link>
        )}
        {document.source_url && (
          <a href={document.source_url} target="_blank" rel="noreferrer noopener" className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
            打开来源
          </a>
        )}
        {canDownload && (
          <a href={`/api/documents/${document.id}/original`} className="rounded border border-blue-600 px-3 py-2 text-sm text-blue-700 hover:bg-blue-50">
            {document.origin?.kind === 'webdav' ? '下载远端原文件' : '下载原文件'}
          </a>
        )}
        <a href={`/api/exports/documents/${document.id}/markdown`} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
          导出 Markdown
        </a>
        <a href={`/api/exports/documents/${document.id}/json`} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
          导出 JSON
        </a>
        {canRetry && (
          <button type="button" onClick={handleRetry} disabled={retrying} className="rounded border border-amber-500 px-3 py-2 text-sm text-amber-700 hover:bg-amber-50 disabled:opacity-50">
            {retrying ? '正在提交…' : '重新处理'}
          </button>
        )}
        <button type="button" onClick={handleDelete} disabled={deleting} className="ml-auto rounded px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50">
          {deleting ? '删除中…' : '删除'}
        </button>
      </div>

      {editingMetadata && (
        <section className="mt-4 rounded-xl border border-blue-200 bg-blue-50/40 p-4">
          <h2 className="font-semibold text-slate-900">编辑整理信息</h2>
          <label className="mt-3 block text-sm text-slate-700">
            标题
            <input value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} className="mt-1 block w-full rounded border border-slate-300 bg-white px-3 py-2" />
          </label>
          <label className="mt-3 block text-sm text-slate-700">
            摘要
            <textarea value={draftSummary} onChange={(event) => setDraftSummary(event.target.value)} rows={4} className="mt-1 block w-full rounded border border-slate-300 bg-white px-3 py-2" />
          </label>
          <div className="mt-3">
            <p className="text-sm text-slate-700">标签</p>
            <div className="mt-2 max-h-40 overflow-y-auto rounded border border-slate-200 bg-white p-2">
              <div className="flex flex-wrap gap-2">
                {tagOptions.map((tag) => {
                  const selected = draftTagIds.includes(tag.id);
                  return (
                    <button key={tag.id} type="button" onClick={() => setDraftTagIds((current) => selected ? current.filter((id) => id !== tag.id) : [...current, tag.id])} className={`rounded-full border px-2 py-1 text-xs ${selected ? 'border-blue-700 bg-blue-700 text-white' : 'border-slate-300 text-slate-600'}`}>
                      #{tag.name}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
          <button type="button" disabled={savingMetadata || !draftTitle.trim()} onClick={handleSaveMetadata} className="mt-4 rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50">
            {savingMetadata ? '保存中…' : '保存整理信息'}
          </button>
        </section>
      )}

      <details className="mt-4 rounded-xl border border-slate-200 bg-white">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 hover:bg-slate-50">
          <div className="min-w-0">
            <span className="text-sm font-semibold text-slate-800">文档信息</span>
            <span className="ml-3 text-xs text-slate-500">{informationSummary}</span>
          </div>
          <span className="shrink-0 text-xs text-slate-400">展开查看与整理</span>
        </summary>
        <div className="border-t border-slate-200 p-4">
          {document.summary?.summary && (
            <div>
              <h2 className="text-xs font-semibold text-slate-500">摘要</h2>
              <p className="mt-2 text-sm leading-6 text-slate-700">{document.summary.summary}</p>
            </div>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
            <span className="text-slate-500">主分类</span>
            {document.primary_category ? (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800">{document.primary_category.name}</span>
            ) : (
              <span className="text-slate-400">未分类</span>
            )}
            <select
              value={selectedCategoryId}
              onChange={event => {
                const value = event.target.value;
                setSelectedCategoryId(value === '' ? '' : Number(value));
                setCategoryMessage('');
              }}
              disabled={savingCategory}
              className="ml-2 rounded border border-slate-300 px-2 py-1.5 text-sm"
            >
              <option value="">选择分类</option>
              {categories.map(category => (
                <option key={category.id} value={category.id}>{category.name}</option>
              ))}
            </select>
            <button type="button" onClick={handleSaveCategory} disabled={savingCategory || selectedCategoryId === ''} className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">
              {savingCategory ? '保存中…' : '保存'}
            </button>
            {categoryMessage && <span className="text-xs text-slate-500">{categoryMessage}</span>}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
            <span className="text-slate-500">标签</span>
            {document.tags.length ? document.tags.map(tag => (
              <span key={tag.id} className="rounded-full border border-slate-200 px-2 py-0.5 text-slate-600">#{tag.name}</span>
            )) : <span className="text-slate-400">无标签</span>}
          </div>
          {(metadata?.author || metadata?.published_at) && (
            <div className="mt-4 text-xs text-slate-500">
              {metadata?.author && <span>作者：{metadata.author}</span>}
              {metadata?.author && metadata?.published_at && <span> · </span>}
              {metadata?.published_at && <span>发布日期：{metadata.published_at}</span>}
            </div>
          )}
          {document.source_url && (
            <div className="mt-4 break-all text-xs">
              <span className="text-slate-500">来源地址：</span>
              <a href={document.source_url} target="_blank" rel="noreferrer noopener" className="font-mono text-blue-600 hover:underline">{document.source_url}</a>
            </div>
          )}
          {document.origin?.kind === 'webdav' && (
            <div className="mt-4 rounded-lg bg-violet-50 p-3 text-sm">
              <span className="text-slate-500">知识来源：</span>
              {document.origin.connector_available && document.origin.connector_id ? (
                <Link href="/settings/sources" className="font-medium text-violet-800 hover:underline">WebDAV · {document.origin.label}</Link>
              ) : (
                <span className="font-medium text-slate-700">WebDAV · {document.origin.label}</span>
              )}
              {document.origin.remote_path && <div className="mt-1 break-all font-mono text-xs text-slate-600">{document.origin.remote_path}</div>}
            </div>
          )}
        </div>
      </details>

      {pipeline && (
        <details
          className="mt-3 rounded-xl border border-slate-200 bg-white"
          open={pipeline.overall_status !== 'completed' || pipelineExpanded}
          onToggle={(event) =>
            setPipelineExpanded(event.currentTarget.open)
          }
        >
          <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 hover:bg-slate-50">
            <div>
              <span className="text-sm font-semibold text-slate-800">处理与检索状态</span>
              <span className="ml-3 text-xs text-slate-500">
                {knowledgeStatus} · {pipeline.stages.chunking.child_chunks} 个切片
              </span>
            </div>
            <span className="shrink-0 text-xs text-slate-400">
              {pipeline.overall_status === 'completed' ? '展开详情' : '处理中，自动展开'}
            </span>
          </summary>
          <PipelineStatus pipeline={pipeline} />
        </details>
      )}

      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
      {latestJob?.last_error && status === 'failed' && (
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          处理失败：{latestJob.last_error}
        </div>
      )}

      {version?.raw_content ? (
        rendersAsMarkdown ? (
          <MarkdownBody content={version.raw_content} />
        ) : (
          <article className="mx-auto mt-6 max-w-4xl whitespace-pre-wrap rounded-2xl border border-slate-200 bg-white p-5 text-[15px] leading-7 text-slate-800 shadow-sm sm:p-9 sm:text-base sm:leading-8">
            {version.raw_content}
          </article>
        )
      ) : (
        <div className="mt-6 rounded-xl border border-slate-200 bg-slate-50 p-4 text-slate-600">
          {['created', 'processing', 'retry'].includes(status)
            ? '正在提取正文，请稍候…'
            : version?.meta?.extraction_message ||
              (status === 'ready'
                ? '处理已经完成，但没有识别到可显示的正文。你可以尝试重新处理。'
                : '暂时没有可显示的正文。')}
        </div>
      )}

    </main>
  );
}

function MarkdownBody({ content }: { content: string }) {
  return (
    <article className="mx-auto mt-6 max-w-4xl rounded-2xl border border-slate-200 bg-white p-5 text-slate-800 shadow-sm sm:p-9">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          h1: ({ children }) => (
            <h1 className="mb-4 mt-1 border-b border-slate-200 pb-2 text-2xl font-bold">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-3 mt-7 text-xl font-semibold">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-2 mt-5 text-lg font-semibold">{children}</h3>
          ),
          p: ({ children }) => (
            <p className="my-3 leading-7 text-slate-700">{children}</p>
          ),
          ul: ({ children }) => (
            <ul className="my-3 list-disc space-y-1 pl-6">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="my-3 list-decimal space-y-1 pl-6">{children}</ol>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-4 border-l-4 border-slate-300 bg-slate-50 px-4 py-1 text-slate-600">
              {children}
            </blockquote>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
              className="text-blue-600 underline decoration-blue-300 underline-offset-2"
            >
              {children}
            </a>
          ),
          code: ({ className, children }) =>
            className ? (
              <code className={`${className} text-sm`}>{children}</code>
            ) : (
              <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-sm text-rose-700">
                {children}
              </code>
            ),
          pre: ({ children }) => (
            <pre className="my-4 overflow-x-auto rounded-lg bg-slate-900 p-4 text-sm leading-6 text-slate-100">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="my-4 overflow-x-auto">
              <table className="min-w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-slate-300 bg-slate-100 px-3 py-2 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-slate-300 px-3 py-2 align-top">{children}</td>
          ),
          hr: () => <hr className="my-6 border-slate-200" />,
        }}
      >
        {content}
      </ReactMarkdown>
    </article>
  );
}

function PipelineStatus({ pipeline }: { pipeline: ProcessingPipeline }) {
  const stages = [
    {
      key: 'parsing',
      title: '正文提取',
      stage: pipeline.stages.parsing,
      detail: '下载并提取可阅读正文',
    },
    {
      key: 'understanding',
      title: 'AI 整理',
      stage: pipeline.stages.understanding,
      detail: '摘要、分类和标签',
    },
    {
      key: 'chunking',
      title: '知识切片',
      stage: pipeline.stages.chunking,
      detail: `已生成 ${pipeline.stages.chunking.child_chunks} 个可检索切片`,
    },
    {
      key: 'embedding',
      title: '向量解析',
      stage: pipeline.stages.embedding,
      detail:
        pipeline.stages.embedding.status === 'disabled'
          ? '未启用向量模型'
          : `${pipeline.stages.embedding.completed}/${pipeline.stages.embedding.total} 个切片${
              pipeline.stages.embedding.model
                ? ` · ${pipeline.stages.embedding.model}`
                : ''
            }`,
    },
  ];

  return (
    <div className="border-t border-slate-200 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-slate-500">
          正文完成后，系统继续进行 AI 整理、切片和向量解析。
        </p>
        <div className="flex flex-wrap gap-2 text-xs">
          <span
            className={`rounded-full px-2 py-1 ${
              pipeline.keyword_searchable
                ? 'bg-emerald-100 text-emerald-800'
                : 'bg-slate-100 text-slate-500'
            }`}
          >
            {pipeline.keyword_searchable ? '可关键词检索' : '尚不可检索'}
          </span>
          <span
            className={`rounded-full px-2 py-1 ${
              pipeline.vector_searchable
                ? 'bg-violet-100 text-violet-800'
                : 'bg-slate-100 text-slate-500'
            }`}
          >
            {pipeline.vector_searchable ? '可语义检索' : '语义检索未就绪'}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {stages.map(({ key, title, stage, detail }) => (
          <div key={key} className="rounded-lg border border-slate-200 p-3">
            <div className="flex items-center gap-2">
              <StageIndicator status={stage.status} />
              <h3 className="text-sm font-medium text-slate-800">{title}</h3>
            </div>
            <p className="mt-2 text-xs text-slate-600">{stage.message}</p>
            <p className="mt-1 text-xs text-slate-400">{detail}</p>
            {stage.last_error && (
              <p className="mt-2 text-xs text-red-700">{stage.last_error}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function StageIndicator({ status }: { status: string }) {
  const styles: Record<string, string> = {
    completed: 'bg-emerald-500',
    disabled: 'bg-slate-300',
    skipped: 'bg-slate-300',
    failed: 'bg-red-500',
    processing: 'animate-pulse bg-blue-500',
    created: 'bg-amber-400',
    retry: 'bg-amber-400',
    pending: 'bg-slate-300',
  };
  return (
    <span
      className={`h-2.5 w-2.5 shrink-0 rounded-full ${
        styles[status] ?? 'bg-slate-300'
      }`}
      aria-hidden="true"
    />
  );
}
