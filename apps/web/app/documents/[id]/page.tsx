'use client';

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

import { withApiBasePath } from '@/lib/paths';
import ChunkingPreview from '@/components/ChunkingPreview';
import KnowledgeEnhancement from '@/components/KnowledgeEnhancement';

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
    preview?: {
      status?: string;
      reason?: string;
      message?: string;
    };
    spreadsheet_processing?: {
      policy_version?: string;
      mode?: 'dataset' | 'mixed' | 'governance' | 'empty';
      dataset_regions?: number;
      governance_regions?: number;
      governance_required?: boolean;
      layout_risks?: string[];
    };
  };
  structured_content: {
    document_type?: string;
    metadata?: {
      author?: string | null;
      published_at?: string | null;
      canonical_url?: string | null;
      regions?: Array<{
        sheet_name?: string;
        region_index?: number;
        row_start?: number;
        row_end?: number;
        dataset_eligible?: boolean;
        layout_risks?: string[];
      }>;
    };
  } | null;
  blob: {
    id: number;
    original_filename: string | null;
    file_size: number;
    content_type: string;
  } | null;
  preview_blob: {
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
  extraction?: {
    version: string | null;
    engine: string | null;
    ocr_status: string | null;
    page_count: number | null;
    native_text_pages: number;
    image_pages: number;
    ocr_candidate_pages: number;
    ocr_completed_pages: number;
    ocr_failed_pages: number[];
    ocr_skipped_pages: number[];
    external_provider?: { provider: string; model: string; version?: string } | null;
    external_attempted_pages?: number;
    external_completed_pages?: number;
    external_failed_pages?: number[];
    external_skipped_pages?: number[];
    external_trigger_reasons?: Record<string, number[]>;
  };
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
  content_kind: 'document' | 'dataset' | 'spreadsheet' | 'note';
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

type DatasetField = {
  id: number;
  position: number;
  name: string;
  inferred_type: string;
  semantic_role: string | null;
  null_count: number;
  distinct_count: number | null;
  sample_values: string[];
  statistics: Record<string, unknown>;
};

type KnowledgeDataset = {
  id: number;
  name: string;
  sheet_name: string;
  region_index: number;
  status: string;
  row_count: number;
  column_count: number;
  source_row_start: number | null;
  source_row_end: number | null;
  profile: { quality?: { completeness?: number; empty_cells?: number } };
  execution: {
    backend: 'duckdb' | 'postgresql_fallback';
    artifact_status: string;
    artifact_version: number | null;
    format: string | null;
    byte_size: number;
  };
  fields: DatasetField[];
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
  const [returnToAsk, setReturnToAsk] = useState(false);
  const [datasets, setDatasets] = useState<KnowledgeDataset[]>([]);
  const [previewMode, setPreviewMode] = useState<'original' | 'parsed'>('parsed');
  const [citationTarget, setCitationTarget] = useState<{
    chunkId: number;
    page: number | null;
    paragraph: number | null;
    content: string | null;
    headingPath: string[];
  } | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams(window.location.search);
      setReturnToAsk(query.get('return_to') === 'ask');
      const chunkId = Number(query.get('chunk_id'));
      if (Number.isInteger(chunkId) && chunkId > 0) {
        const page = Number(query.get('page'));
        const paragraph = query.has('paragraph') ? Number(query.get('paragraph')) : null;
        setCitationTarget({
          chunkId,
          page: Number.isInteger(page) && page > 0 ? page : null,
          paragraph: paragraph !== null && Number.isInteger(paragraph) && paragraph >= 0 ? paragraph : null,
          content: null,
          headingPath: [],
        });
        void fetch(`/api/v1/knowledge/chunks/${chunkId}`, { cache: 'no-store' })
          .then((response) => response.ok ? response.json() : Promise.reject(new Error()))
          .then((chunk: { content?: string; heading_path?: string[] }) => {
            setCitationTarget((current) => current?.chunkId === chunkId ? {
              ...current,
              content: chunk.content ?? null,
              headingPath: chunk.heading_path ?? [],
            } : current);
          })
          .catch(() => undefined);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

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
      const [documentResponse, jobResponse, pipelineResponse, categoriesResponse, tagsResponse, datasetsResponse] =
        await Promise.all([
          fetch(`/api/documents/${params.id}`, { cache: 'no-store' }),
          fetch(`/api/documents/${params.id}/latest-job`, { cache: 'no-store' }),
          fetch(`/api/documents/${params.id}/processing-status`, {
            cache: 'no-store',
          }),
          fetch(`/api/categories`, { cache: 'no-store' }),
          fetch(`/api/tags`, { cache: 'no-store' }),
          fetch(`/api/datasets?document_id=${params.id}`, { cache: 'no-store' }),
        ]);
      if (!documentResponse.ok) {
        throw new Error(documentResponse.status === 404 ? '资料不存在' : '读取失败');
      }
      if (!jobResponse.ok) throw new Error('读取处理状态失败');
      if (!pipelineResponse.ok) throw new Error('读取处理进度失败');
      if (!categoriesResponse.ok) throw new Error('读取分类失败');
      if (!tagsResponse.ok) throw new Error('读取标签失败');
      if (!datasetsResponse.ok) throw new Error('读取数据集失败');
      const documentBody = (await documentResponse.json()) as Document;
      setDocument(documentBody);
      const filename = (
        documentBody.current_version?.blob?.original_filename ??
        documentBody.origin?.remote_path ??
        ''
      ).toLowerCase();
      const contentType = documentBody.current_version?.blob?.content_type?.toLowerCase() ?? '';
      if (
        filename.endsWith('.pdf') ||
        contentType === 'application/pdf' ||
        documentBody.current_version?.preview_blob
      ) {
        setPreviewMode('original');
      }
      setLatestJob(await jobResponse.json());
      setPipeline((await pipelineResponse.json()) as ProcessingPipeline);
      const categoryBody = (await categoriesResponse.json()) as CategoryOption[];
      setCategories(categoryBody);
      setTagOptions((await tagsResponse.json()) as TagOption[]);
      setDatasets((await datasetsResponse.json()) as KnowledgeDataset[]);
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
  const spreadsheetProcessing = version?.meta?.spreadsheet_processing;
  const spreadsheetNeedsGovernance = Boolean(
    spreadsheetProcessing?.governance_required,
  );
  const spreadsheetOnlyNeedsGovernance = ['governance', 'empty'].includes(
    spreadsheetProcessing?.mode || '',
  );
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
  const originalFilename = (
    version?.blob?.original_filename ?? document.origin?.remote_path ?? ''
  ).toLowerCase();
  const originalContentType = version?.blob?.content_type?.toLowerCase() ?? '';
  const isPdf =
    originalFilename.endsWith('.pdf') || originalContentType === 'application/pdf';
  const isWord = /\.docx?$/.test(originalFilename) ||
    originalContentType.includes('wordprocessingml') ||
    originalContentType === 'application/msword';
  const hasConvertedPreview = Boolean(version?.preview_blob);
  const hasPaginatedPreview = isPdf || hasConvertedPreview;
  const previewUrl = isPdf
    ? withApiBasePath(`/api/documents/${document.id}/original?inline=true`)
    : withApiBasePath(`/api/documents/${document.id}/preview`);
  const knowledgeStatus = spreadsheetOnlyNeedsGovernance
    ? '等待数据治理'
    : pipeline?.overall_status === 'completed'
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
    <main className="document-shell mx-auto max-w-6xl px-4 sm:px-6">
      <Link
        href={returnToAsk ? '/ask' : '/documents'}
        className="text-sm font-medium text-slate-500 hover:text-slate-900"
      >
        {returnToAsk ? '← 返回当前对话' : '← 返回知识库'}
      </Link>

      {returnToAsk && (
        <Link
          href="/ask"
          className="fixed left-1/2 bottom-[max(1rem,env(safe-area-inset-bottom))] z-30 -translate-x-1/2 rounded-full border border-slate-300 bg-white/95 px-5 py-3 text-sm font-semibold text-slate-800 shadow-xl shadow-slate-950/15 backdrop-blur sm:hidden"
        >
          ← 返回当前对话
        </Link>
      )}

      <div className="mt-4">
        <h1 className="max-w-5xl text-2xl font-semibold text-slate-950 sm:text-3xl">{document.title}</h1>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <span className={`rounded-full bg-slate-100 px-2.5 py-1 ${statusColors[status] || 'text-slate-700'}`}>
          正文{statusLabels[status] || status}
        </span>
        {document.content_kind === 'dataset' && (
          <span className="rounded-full bg-cyan-100 px-2.5 py-1 text-cyan-800">结构化数据集</span>
        )}
        {document.content_kind === 'spreadsheet' && (
          <span className="rounded-full bg-amber-100 px-2.5 py-1 text-amber-800">待治理表格</span>
        )}
        {pipeline && (
          <span
            className={`rounded-full px-2.5 py-1 ${
              spreadsheetOnlyNeedsGovernance
                ? 'bg-amber-100 text-amber-800'
                : pipeline.overall_status === 'completed'
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

      {spreadsheetNeedsGovernance && (
        <section className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-950">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-700">Spreadsheet governance</p>
          <h2 className="mt-1 text-lg font-semibold">这个工作簿没有被强行解释为二维数据集</h2>
          <p className="mt-2 text-sm leading-6 text-amber-900/90">
            原文件、单元格坐标和结构诊断已经保留；无法可靠识别的区域不会进入全文切片或向量索引。
            请先整理为稳定表头和逐行记录，再重新上传治理后的版本。
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <span className="rounded-full bg-white/80 px-2.5 py-1">可查询区域 {spreadsheetProcessing?.dataset_regions ?? 0}</span>
            <span className="rounded-full bg-white/80 px-2.5 py-1">待治理区域 {spreadsheetProcessing?.governance_regions ?? 0}</span>
            {(spreadsheetProcessing?.layout_risks || []).map((risk) => (
              <span key={risk} className="rounded-full bg-amber-100 px-2.5 py-1">{spreadsheetRiskLabel(risk)}</span>
            ))}
          </div>
        </section>
      )}

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
          <a href={withApiBasePath(`/api/documents/${document.id}/original`)} className="rounded border border-blue-600 px-3 py-2 text-sm text-blue-700 hover:bg-blue-50">
            {document.origin?.kind === 'webdav' ? '下载远端原文件' : '下载原文件'}
          </a>
        )}
        <a href={withApiBasePath(`/api/exports/documents/${document.id}/markdown`)} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
          导出 Markdown
        </a>
        <a href={withApiBasePath(`/api/exports/documents/${document.id}/json`)} className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
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
          {!spreadsheetProcessing && (
            <ChunkingPreview key={params.id} documentId={params.id} />
          )}
        </details>
      )}

      <KnowledgeEnhancement key={params.id} docId={params.id} />
      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
      {latestJob?.last_error && status === 'failed' && (
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          处理失败：{latestJob.last_error}
        </div>
      )}

      {citationTarget && (
        <section id="citation-focus" className="mt-5 rounded-2xl border border-amber-300 bg-amber-50 p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-amber-950">引用原文定位</h2>
            <span className="text-xs text-amber-800">
              {citationTarget.headingPath.length > 0 ? citationTarget.headingPath.join(' › ') : '当前文档'}
              {citationTarget.page ? ` · 第 ${citationTarget.page} 页` : ''}
              {citationTarget.paragraph !== null ? ` · 第 ${citationTarget.paragraph + 1} 段` : ''}
            </span>
          </div>
          {citationTarget.content ? (
            <blockquote className="mt-3 whitespace-pre-wrap border-l-4 border-amber-400 pl-4 text-sm leading-7 text-slate-800">
              <mark className="bg-amber-200/80 text-inherit">{citationTarget.content}</mark>
            </blockquote>
          ) : (
            <p className="mt-2 text-xs text-amber-800">正在读取引用段落…</p>
          )}
        </section>
      )}

      {hasPaginatedPreview && (
        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-slate-900">文档预览</h2>
              <p className="mt-1 text-xs text-slate-500">
                {hasConvertedPreview && !isPdf
                  ? '版式预览由原 Word 转换为 PDF；解析版用于检索核对。'
                  : '原文版保持 PDF 排版；解析版用于检索核对。'}
              </p>
            </div>
            <div className="flex rounded-lg bg-slate-100 p-1 text-sm">
              <button type="button" onClick={() => setPreviewMode('original')} className={`rounded-md px-3 py-1.5 ${previewMode === 'original' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>
                {hasConvertedPreview && !isPdf ? '版式预览' : '原文版'}
              </button>
              <button type="button" onClick={() => setPreviewMode('parsed')} className={`rounded-md px-3 py-1.5 ${previewMode === 'parsed' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>解析版</button>
            </div>
          </div>
          {previewMode === 'original' && (
            <iframe
              title={`${document.title} 原文预览`}
              src={`${previewUrl}${citationTarget?.page ? `#page=${citationTarget.page}` : ''}`}
              className="h-[72vh] min-h-[560px] w-full rounded-2xl border border-slate-200 bg-slate-100 shadow-sm"
            />
          )}
        </section>
      )}

      {isWord && !hasConvertedPreview && (
        <div className="mt-6 rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
          {version?.meta?.preview?.reason === 'webdav_source_unavailable'
            ? '远端 Word 原文件当前已无法获取，因此只能显示之前保存的解析文本。远端文件恢复后重新处理即可生成版式预览。'
            : '当前显示的是 Word 解析版，PDF 版式预览正在等待生成；原文件不会被替换。'}
        </div>
      )}

      {hasPaginatedPreview && previewMode === 'original' ? null : datasets.length > 0 ? (
        <DatasetWorkspace datasets={datasets} />
      ) : document.content_kind === 'spreadsheet' ? (
        <SpreadsheetGovernanceSummary processing={spreadsheetProcessing} />
      ) : version?.raw_content ? (
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

function spreadsheetRiskLabel(risk: string) {
  const labels: Record<string, string> = {
    merged_cells: '存在合并单元格',
    mixed_single_cell_rows: '标题/说明与数据混排',
    unconfirmed_header: '未确认稳定表头',
    separated_columns: '存在并排区域',
    summary_rows: '明细与汇总混排',
  };
  return labels[risk] || risk;
}

function SpreadsheetGovernanceSummary({ processing }: { processing: DocumentVersion['meta']['spreadsheet_processing'] }) {
  return (
    <section className="mx-auto mt-6 max-w-4xl rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-slate-900">表格治理建议</h2>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        藏知目前只把可验证的二维区域作为数据集处理。这个文件仍可下载和整理，
        但不会把复杂排版转换成长文本后参与知识问答。
      </p>
      <ul className="mt-4 list-disc space-y-2 pl-5 text-sm text-slate-700">
        <li>每个数据区域保留一组稳定、唯一的字段名。</li>
        <li>标题、单位和备注放在数据区域之外，或放入明确的备注列。</li>
        <li>一行表示一条记录，避免把层级、包含项和不包括项只编码在缩进或符号中。</li>
        <li>治理完成后作为新版本重新上传，原文件不会被覆盖。</li>
      </ul>
      {processing?.mode === 'mixed' && (
        <p className="mt-4 rounded-lg bg-cyan-50 p-3 text-sm text-cyan-900">
          当前工作簿同时包含可查询区域；这些区域仍可在上方数据集工作区使用。
        </p>
      )}
    </section>
  );
}

function DatasetWorkspace({ datasets }: { datasets: KnowledgeDataset[] }) {
  const [selectedId, setSelectedId] = useState(datasets[0]?.id ?? 0);
  const [tab, setTab] = useState<'data' | 'fields'>('data');
  const [offset, setOffset] = useState(0);
  const [preview, setPreview] = useState<{
    total: number;
    columns: string[];
    rows: Array<Record<string, unknown>>;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pageSize = 50;
  const dataset = datasets.find((item) => item.id === selectedId) ?? datasets[0];

  useEffect(() => {
    if (!dataset) return;
    const controller = new AbortController();
    fetch(`/api/datasets/${dataset.id}/rows?offset=${offset}&limit=${pageSize}`, {
      cache: 'no-store',
      signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('数据预览读取失败')))
      .then((body) => {
        setPreview(body);
        setError('');
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : '数据预览读取失败');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [dataset, offset]);

  if (!dataset) return null;
  const completeness = dataset.profile.quality?.completeness;
  const executionLabel = dataset.execution?.backend === 'duckdb'
    ? `DuckDB · Parquet v${dataset.execution.artifact_version ?? '-'}`
    : dataset.execution?.artifact_status === 'failed'
      ? '列式构建失败 · 兼容模式'
      : '列式构建中 · 兼容模式';
  const typeLabels: Record<string, string> = {
    text: '文本', number: '数值', date: '日期', boolean: '布尔', identifier: '标识符', unknown: '待分析',
  };

  return (
    <section className="mt-6 overflow-hidden rounded-2xl border border-cyan-200 bg-white shadow-sm">
      <div className="border-b border-slate-200 bg-gradient-to-r from-cyan-50 to-white p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-cyan-700">Queryable dataset</p>
            <h2 className="mt-1 text-xl font-semibold text-slate-950">数据集工作区</h2>
            <p className="mt-1 text-sm text-slate-600">表格使用结构化查询，不再把全部内容作为长文档渲染。</p>
          </div>
          {datasets.length > 1 && (
            <select value={dataset.id} onChange={(event) => { setLoading(true); setSelectedId(Number(event.target.value)); setOffset(0); }} className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">
              {datasets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          )}
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <DatasetMetric label="数据行" value={dataset.row_count.toLocaleString('zh-CN')} />
          <DatasetMetric label="字段" value={String(dataset.column_count)} />
          <DatasetMetric label="完整度" value={typeof completeness === 'number' ? `${(completeness * 100).toFixed(1)}%` : '待分析'} />
          <DatasetMetric label="查询执行" value={executionLabel} />
          <DatasetMetric label="来源位置" value={`${dataset.sheet_name} · ${dataset.source_row_start ?? '-'}–${dataset.source_row_end ?? '-'}`} />
        </div>
      </div>
      <div className="flex gap-1 border-b border-slate-200 px-4 pt-3">
        {([['data', '数据预览'], ['fields', '字段画像']] as const).map(([key, label]) => (
          <button key={key} type="button" onClick={() => setTab(key)} className={`rounded-t-lg px-4 py-2 text-sm ${tab === key ? 'border border-b-white border-slate-200 bg-white font-medium text-slate-900 -mb-px' : 'text-slate-500'}`}>{label}</button>
        ))}
      </div>
      {tab === 'data' ? (
        <div className="p-4">
          {loading && <p className="py-8 text-center text-sm text-slate-500">正在读取当前页…</p>}
          {error && <p className="py-4 text-sm text-red-700">{error}</p>}
          {!loading && preview && (
            <>
              <div className="overflow-x-auto rounded-xl border border-slate-200">
                <table className="min-w-full whitespace-nowrap text-left text-sm">
                  <thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="px-3 py-2">原始行</th>{preview.columns.map((column) => <th key={column} className="px-3 py-2 font-medium">{column}</th>)}</tr></thead>
                  <tbody className="divide-y divide-slate-100">{preview.rows.map((row, index) => <tr key={`${String(row.row_number)}-${index}`} className="hover:bg-cyan-50/40"><td className="px-3 py-2 font-mono text-xs text-slate-400">{String(row.row_number ?? '')}</td>{preview.columns.map((column) => <td key={column} className="max-w-72 truncate px-3 py-2 text-slate-700" title={String(row[column] ?? '')}>{String(row[column] ?? '') || '—'}</td>)}</tr>)}</tbody>
                </table>
              </div>
              <div className="mt-3 flex items-center justify-between text-sm text-slate-500">
                <span>第 {offset + 1}–{Math.min(offset + pageSize, preview.total)} 行，共 {preview.total.toLocaleString('zh-CN')} 行</span>
                <div className="flex gap-2"><button type="button" disabled={offset === 0} onClick={() => { setLoading(true); setOffset(Math.max(0, offset - pageSize)); }} className="rounded border border-slate-300 px-3 py-1.5 disabled:opacity-40">上一页</button><button type="button" disabled={offset + pageSize >= preview.total} onClick={() => { setLoading(true); setOffset(offset + pageSize); }} className="rounded border border-slate-300 px-3 py-1.5 disabled:opacity-40">下一页</button></div>
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="grid gap-3 p-4 md:grid-cols-2">
          {dataset.fields.map((field) => (
            <div key={field.id} className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between gap-2"><h3 className="font-medium text-slate-900">{field.name}</h3><span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">{typeLabels[field.inferred_type] || field.inferred_type}</span></div>
              <p className="mt-2 text-xs text-slate-500">空值 {field.null_count.toLocaleString('zh-CN')} · 唯一值 {field.distinct_count === null ? '较多' : field.distinct_count.toLocaleString('zh-CN')}{field.semantic_role ? ` · ${field.semantic_role}` : ''}</p>
              {field.sample_values.length > 0 && <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-600">样例：{field.sample_values.slice(0, 5).join('、')}</p>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function DatasetMetric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl border border-white/80 bg-white/80 p-3"><p className="text-xs text-slate-500">{label}</p><p className="mt-1 truncate text-sm font-semibold text-slate-900" title={value}>{value}</p></div>;
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
      {pipeline.stages.parsing.extraction && (
        <PdfExtractionSummary extraction={pipeline.stages.parsing.extraction} />
      )}
    </div>
  );
}

function PdfExtractionSummary({
  extraction,
}: {
  extraction: NonNullable<PipelineStage['extraction']>;
}) {
  const failedPages = extraction.ocr_failed_pages.length;
  const skippedPages = extraction.ocr_skipped_pages.length;
  const completed = extraction.ocr_completed_pages;
  const native = extraction.native_text_pages;
  const candidates = extraction.ocr_candidate_pages;
  const statusLabel: Record<string, string> = {
    completed: 'OCR 已完成',
    partial: 'OCR 部分完成',
    failed: 'OCR 全部失败',
    skipped: '已跳过 OCR',
    not_needed: '无需 OCR',
  };
  const statusText = extraction.ocr_status
    ? statusLabel[extraction.ocr_status] ?? extraction.ocr_status
    : '无需 OCR';

  const externalAttempted = extraction.external_attempted_pages ?? 0;
  const externalCompleted = extraction.external_completed_pages ?? 0;
  const externalFailed = extraction.external_failed_pages ?? [];
  const externalSkipped = extraction.external_skipped_pages ?? [];
  const externalProvider = extraction.external_provider;
  const externalTriggers = extraction.external_trigger_reasons ?? {};
  const triggerEntries = Object.entries(externalTriggers);

  return (
    <div className="mt-4 rounded-lg border border-slate-200 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-slate-800">PDF 解析摘要</h4>
        <span className="text-xs text-slate-500">
          {statusText}
          {extraction.engine ? ` · ${extraction.engine}` : ''}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-600 sm:grid-cols-4">
        <div>
          <dt className="text-slate-400">总页数</dt>
          <dd className="mt-1 text-slate-700">{extraction.page_count ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-slate-400">原生文字页</dt>
          <dd className="mt-1 text-slate-700">{native}</dd>
        </div>
        <div>
          <dt className="text-slate-400">OCR 完成</dt>
          <dd className="mt-1 text-slate-700">{completed}</dd>
        </div>
        <div>
          <dt className="text-slate-400">OCR 候选</dt>
          <dd className="mt-1 text-slate-700">{candidates}</dd>
        </div>
      </dl>
      {(failedPages > 0 || skippedPages > 0) && (
        <p className="mt-3 text-xs text-slate-500">
          OCR 失败 {failedPages} 页 · 跳过 {skippedPages} 页。这些页的图片文字尚未入库，
          但不影响其他页面继续处理。
        </p>
      )}
      {externalProvider?.provider && externalAttempted > 0 && (
        <div className="mt-3 rounded border border-emerald-100 bg-emerald-50/40 p-3 text-xs text-emerald-900">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">
              外部视觉识别
              {externalProvider.model ? ` · ${externalProvider.model}` : ''}
            </p>
            <span className="text-emerald-700">
              触发 {externalAttempted} 页 · 完成 {externalCompleted} 页
            </span>
          </div>
          {triggerEntries.length > 0 && (
            <ul className="mt-2 list-disc pl-4 text-emerald-800">
              {triggerEntries.map(([reason, pages]) => (
                <li key={reason}>
                  {triggerLabel(reason)}：{pages.length} 页
                </li>
              ))}
            </ul>
          )}
          {(externalFailed.length > 0 || externalSkipped.length > 0) && (
            <p className="mt-2 text-emerald-800">
              外部失败 {externalFailed.length} 页 · 降级本地 {externalSkipped.length} 页。
              本地识别仍会保留可读结果。
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function triggerLabel(reason: string): string {
  const labels: Record<string, string> = {
    no_text: '本地未识别到文字',
    low_chars: '本地识别字符过少',
    low_confidence: '本地置信度不足',
    max_external_pages_reached: '已达单文档外发页数上限',
    manual: '手动触发',
  };
  return labels[reason] ?? reason;
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
