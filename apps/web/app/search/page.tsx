'use client';

import { Suspense, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { SEARCH_PAGE_SIZE, readSearchLocation, searchLocationHref, searchEvidenceHref } from '@/lib/ask-search-state';
import { withBasePath } from '@/lib/paths';
import { KnowledgePreview, type KnowledgePreviewTarget } from '@/components/knowledge-preview';

type Highlight = { start: number; end: number };

type CategoryOption = {
  id: number;
  slug: string;
  name: string;
  document_count: number;
};

type TagOption = {
  id: number;
  slug: string;
  name: string;
  document_count: number;
};

type SourceTypeOption = {
  value: string;
  label: string;
};

type FiltersPayload = {
  source_types: SourceTypeOption[];
  categories: CategoryOption[];
  tags: TagOption[];
};

type SearchHit = {
  document_id: number;
  document_version_id: number;
  title: string;
  source_type: string;
  source_url: string | null;
  score: number;
  snippet: string;
  highlights: Highlight[];
  chunk: {
    id: number;
    parent_id: number | null;
    type: string;
    heading_path: string[];
    page: number | null;
    paragraph_index: number | null;
    source_start: number | null;
    source_end: number | null;
  };
  categories: { id: number; slug: string; name: string }[];
  tags: { id: number; slug: string; name: string }[];
  supporting_evidence?: {
    document_id: number;
    document_version_id: number;
    chunk: SearchHit['chunk'];
    snippet: string;
    context: string | null;
  }[];
};

type SearchResponse = {
  query: string;
  backend: string;
  total: number;
  limit: number;
  offset: number;
  hits: SearchHit[];
  retrieval?: {
    mode: string;
    vector_used: boolean;
    degraded_reason: string | null;
    active_profile_id: number | null;
  };
};

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; payload: SearchResponse };

export default function SearchPage() {
  return (
    <Suspense fallback={<SearchSkeleton />}>
      <SearchClient />
    </Suspense>
  );
}

function SearchSkeleton() {
  return (
    <main className="container mx-auto max-w-5xl p-4">
      <p className="text-sm text-slate-500">加载中…</p>
    </main>
  );
}

function SearchClient() {
  const searchParams = useSearchParams();
  const initial = readSearchLocation(new URLSearchParams(searchParams.toString()));
  const [query, setQuery] = useState(initial.query);
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  const [categorySlugs, setCategorySlugs] = useState<string[]>(initial.categorySlugs);
  const [tagSlugs, setTagSlugs] = useState<string[]>(initial.tagSlugs);
  const [sourceTypes, setSourceTypes] = useState<string[]>(initial.sourceTypes);
  const [page, setPage] = useState(initial.page);
  const writtenLocationRef = useRef(searchParams.toString());
  const restoringLocationRef = useRef<string | null>(null);
  const [filters, setFilters] = useState<FiltersPayload | null>(null);
  const [filtersError, setFiltersError] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: 'idle' });
  const [retryNonce, setRetryNonce] = useState(0);
  const [previewTarget, setPreviewTarget] = useState<KnowledgePreviewTarget | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const previewTriggerRef = useRef<HTMLButtonElement | null>(null);
  const previewScrollRef = useRef(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const hasAnyFilter =
    categorySlugs.length > 0 || tagSlugs.length > 0 || sourceTypes.length > 0;

  useEffect(() => {
    // Browser back/forward and links restore the entire search, not only its text.
    // Next can expose the destination query before this outgoing page unmounts.
    if (window.location.pathname.replace(/\/$/, '') !== withBasePath('/search')) return;
    const incoming = searchParams.toString();
    if (incoming === writtenLocationRef.current) return;
    writtenLocationRef.current = incoming;
    const location = readSearchLocation(new URLSearchParams(incoming));
    restoringLocationRef.current = searchLocationHref(location);
    setQuery(location.query);
    setDebouncedQuery(location.query.trim());
    setCategorySlugs(location.categorySlugs);
    setTagSlugs(location.tagSlugs);
    setSourceTypes(location.sourceTypes);
    setPage(location.page);
  }, [searchParams]);

  useEffect(() => {
    fetch('/api/search/filters', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error('暂时无法读取筛选条件');
        return res.json();
      })
      .then((data: FiltersPayload) => {
        setFilters(data);
        setFiltersError(null);
      })
      .catch((err) => {
        setFiltersError(err instanceof Error ? err.message : '读取筛选条件失败');
      });
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      const trimmed = query.trim();
      if (trimmed !== debouncedQuery) setPage(1);
      setDebouncedQuery(trimmed);
      if (!trimmed && !hasAnyFilter) {
        setState({ kind: 'idle' });
      }
    }, 250);
    return () => window.clearTimeout(handle);
  }, [query, debouncedQuery, hasAnyFilter]);

  useEffect(() => {
    // A queued debounce/effect must never take the URL back from an in-flight
    // browser traversal. The mounted component alone does not own that URL.
    if (window.location.pathname.replace(/\/$/, '') !== withBasePath('/search')) return;
    const href = searchLocationHref({ query: debouncedQuery, categorySlugs, tagSlugs, sourceTypes, page });
    if (restoringLocationRef.current && restoringLocationRef.current !== href) return;
    restoringLocationRef.current = null;
    writtenLocationRef.current = href.split('?')[1] ?? '';
    const browserHref = withBasePath(href);
    if (`${window.location.pathname}${window.location.search}` !== browserHref) {
      window.history.replaceState(null, '', browserHref);
    }
    if (!debouncedQuery && !hasAnyFilter) {
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    const run = async () => {
      setState({ kind: 'loading' });
      try {
        const response = await fetch('/api/search', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: debouncedQuery,
            limit: SEARCH_PAGE_SIZE,
            offset: (page - 1) * SEARCH_PAGE_SIZE,
            category_slugs: categorySlugs,
            tag_slugs: tagSlugs,
            source_types: sourceTypes,
          }),
          signal: controller.signal,
        });
        if (cancelled) {
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || '搜索请求失败');
        }
        const payload = (await response.json()) as SearchResponse;
        if (cancelled) {
          return;
        }
        setState({ kind: 'ready', payload });
      } catch (err) {
        if (cancelled) {
          return;
        }
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : '搜索失败',
        });
      }
    };
    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [
    debouncedQuery,
    categorySlugs,
    tagSlugs,
    sourceTypes,
    retryNonce,
    hasAnyFilter,
    page,
  ]);

  const toggleCategory = (slug: string) => {
    setPage(1);
    setCategorySlugs((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  };
  const toggleTag = (slug: string) => {
    setPage(1);
    setTagSlugs((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  };
  const toggleSourceType = (value: string) => {
    setPage(1);
    setSourceTypes((prev) =>
      prev.includes(value) ? prev.filter((s) => s !== value) : [...prev, value],
    );
  };

  const openPreview = (hit: SearchHit, trigger: HTMLButtonElement) => {
    previewTriggerRef.current = trigger;
    previewScrollRef.current = window.scrollY;
    setPreviewTarget({ documentId: hit.document_id, versionId: hit.document_version_id, chunkId: hit.chunk.id, title: hit.title, sourceType: hit.source_type, sourceUrl: hit.source_url, page: hit.chunk.page });
    setPreviewOpen(true);
  };
  const closePreview = () => {
    setPreviewOpen(false);
    window.setTimeout(() => {
      if (previewTriggerRef.current?.isConnected) {
        previewTriggerRef.current.focus({ preventScroll: true });
        if (window.matchMedia('(max-width: 1023px)').matches) window.scrollTo({ top: previewScrollRef.current });
      }
      else searchInputRef.current?.focus();
    }, 0);
  };

  return (
    <main className="mx-auto w-full max-w-[1600px] p-4 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">搜索资料</h1>
          <p className="mt-1 text-sm text-slate-500">在左侧找资料，在阅读区核对原文；需要分析时，可只针对选中的资料提问。</p>
        </div>
        {previewTarget && <button type="button" onClick={() => previewOpen ? closePreview() : setPreviewOpen(true)} aria-expanded={previewOpen} aria-controls="knowledge-preview" className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-600 hover:bg-slate-50">
          {previewOpen ? '收起阅读区' : '展开阅读区'}
        </button>}
      </div>
      {previewTarget && <div className="mt-4 flex gap-1 rounded-xl bg-slate-100 p-1 lg:hidden" aria-label="搜索工作区视图">
        <button type="button" aria-pressed={!previewOpen} onClick={closePreview} className={`flex-1 rounded-lg px-3 py-2 text-sm ${!previewOpen ? 'bg-white font-medium shadow-sm' : 'text-slate-500'}`}>搜索列表</button>
        <button type="button" aria-pressed={previewOpen} onClick={() => setPreviewOpen(true)} className={`min-w-0 flex-1 truncate rounded-lg px-3 py-2 text-sm ${previewOpen ? 'bg-white font-medium shadow-sm' : 'text-slate-500'}`}>阅读 · {previewTarget.title}</button>
      </div>}
      <div className={`mt-4 grid items-start gap-5 ${previewOpen && previewTarget ? 'lg:grid-cols-[minmax(340px,0.85fr)_minmax(0,1.15fr)]' : ''}`}>
      <div className={`min-w-0 ${previewOpen && previewTarget ? 'hidden lg:block' : 'mx-auto w-full max-w-5xl'}`}>
      <Link
        href={debouncedQuery ? `/ask?q=${encodeURIComponent(debouncedQuery)}` : '/ask'}
        className="mt-4 inline-flex rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:border-slate-300 hover:bg-slate-50"
      >
        {debouncedQuery ? '基于当前关键词提问 →' : '需要归纳分析？前往问知识库 →'}
      </Link>

      <form
        className="mt-6"
        onSubmit={(event) => {
          event.preventDefault();
          setPage(1);
          setDebouncedQuery(query.trim());
          setRetryNonce((value) => value + 1);
        }}
      >
        <label htmlFor="q" className="sr-only">
          搜索关键词
        </label>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            ref={searchInputRef}
            id="q"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：向量检索、合同条款、面试笔记"
            className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
            autoFocus
          />
          <button
            type="submit"
            className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800"
            disabled={!query.trim()}
          >
            搜索
          </button>
          {hasAnyFilter && (
            <button
              type="button"
              onClick={() => {
                setCategorySlugs([]);
                setTagSlugs([]);
                setSourceTypes([]);
                setPage(1);
              }}
              className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50"
            >
              清除筛选
            </button>
          )}
        </div>
      </form>

      {filtersError && (
        <p className="mt-3 text-xs text-red-700">筛选条件加载失败：{filtersError}</p>
      )}

      <FilterSections
        filters={filters}
        categorySlugs={categorySlugs}
        tagSlugs={tagSlugs}
        sourceTypes={sourceTypes}
        onToggleCategory={toggleCategory}
        onToggleTag={toggleTag}
        onToggleSourceType={toggleSourceType}
      />

      <section className="mt-6">
        {!debouncedQuery && !hasAnyFilter && (
          <EmptyState
            heading="开始一次搜索"
            description="支持中文、英文和数字。可以同时选择分类、标签、来源类型来缩小范围。"
          />
        )}
        {(debouncedQuery || hasAnyFilter) && state.kind === 'loading' && (
          <p className="text-sm text-slate-500">正在搜索…</p>
        )}
        {(debouncedQuery || hasAnyFilter) && state.kind === 'error' && (
          <ErrorState
            message={state.message}
            onRetry={() => setRetryNonce((value) => value + 1)}
          />
        )}
        {(debouncedQuery || hasAnyFilter) && state.kind === 'ready' && (
          <>
            {state.payload.hits.length === 0 ? (
              <EmptyState
                heading={page > 1 ? '这一页没有更多结果' : '没有找到匹配的资料'}
                description={page > 1 ? '可以返回上一页，或缩小范围重新搜索。关键词检索只返回有界候选，不代表已遍历全部资料。' : '试试调整关键词、清空筛选，或确认相关资料已经被成功解析。'}
              />
            ) : <Results payload={state.payload} onPreview={openPreview} selectedChunkId={previewTarget?.chunkId ?? null} previewOpen={previewOpen} />}
            <nav aria-label="搜索结果分页" className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs text-slate-500">第 {page} 页 · 每页最多 {SEARCH_PAGE_SIZE} 条</p>
              <div className="flex gap-2">
                <button type="button" disabled={page <= 1} onClick={() => setPage((current) => current - 1)} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">上一页</button>
                <button type="button" disabled={state.payload.hits.length === 0 || state.payload.offset + state.payload.hits.length >= state.payload.total || (state.payload.retrieval?.mode !== 'filters' && !state.payload.retrieval?.vector_used && state.payload.offset + state.payload.hits.length >= 50) || page >= 501} onClick={() => setPage((current) => current + 1)} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">下一页</button>
              </div>
            </nav>
          </>
        )}
      </section>
      </div>
      {previewOpen && previewTarget && <div className="min-w-0 lg:sticky lg:top-4">
        <KnowledgePreview target={previewTarget} onClose={closePreview} className="max-h-[calc(100dvh-10rem)] min-h-[28rem] lg:max-h-[calc(100dvh-6rem)]" />
      </div>}
      </div>
    </main>
  );
}

function FilterSections({
  filters,
  categorySlugs,
  tagSlugs,
  sourceTypes,
  onToggleCategory,
  onToggleTag,
  onToggleSourceType,
}: {
  filters: FiltersPayload | null;
  categorySlugs: string[];
  tagSlugs: string[];
  sourceTypes: string[];
  onToggleCategory: (slug: string) => void;
  onToggleTag: (slug: string) => void;
  onToggleSourceType: (value: string) => void;
}) {
  if (!filters) return null;
  const hasCategories = filters.categories.length > 0;
  const hasTags = filters.tags.length > 0;
  if (!hasCategories && !hasTags && filters.source_types.length === 0) {
    return null;
  }
  return (
    <div className="mt-5 grid items-start gap-4 md:grid-cols-2">
      {filters.source_types.length > 0 && (
        <FilterCard title="来源类型">
          {filters.source_types.map((option) => (
            <FilterChip
              key={option.value}
              label={option.label}
              active={sourceTypes.includes(option.value)}
              onClick={() => onToggleSourceType(option.value)}
            />
          ))}
        </FilterCard>
      )}
      {hasCategories && (
        <FilterCard title="分类">
          {filters.categories
            .filter((cat) => cat.document_count > 0)
            .map((cat) => (
              <FilterChip
                key={cat.slug}
                label={`${cat.name}（${cat.document_count}）`}
                active={categorySlugs.includes(cat.slug)}
                onClick={() => onToggleCategory(cat.slug)}
              />
            ))}
          {filters.categories.every((cat) => cat.document_count === 0) && (
            <p className="text-xs text-slate-500">还没有资料命中分类。</p>
          )}
        </FilterCard>
      )}
      {hasTags && (
        <div className="md:col-span-2">
          <FilterCard title="标签">
            {filters.tags
              .filter((tag) => tag.document_count > 0)
              .slice(0, 30)
              .map((tag) => (
                <FilterChip
                  key={tag.slug}
                  label={`#${tag.name}（${tag.document_count}）`}
                  active={tagSlugs.includes(tag.slug)}
                  onClick={() => onToggleTag(tag.slug)}
                />
              ))}
            {filters.tags.every((tag) => tag.document_count === 0) && (
              <p className="text-xs text-slate-500">还没有资料带标签。</p>
            )}
          </FilterCard>
        </div>
      )}
    </div>
  );
}

function FilterCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h2>
      <div className="mt-2 flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  const base =
    'cursor-pointer rounded-full border px-3 py-1 text-xs transition-colors';
  const style = active
    ? 'border-slate-900 bg-slate-900 text-white'
    : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50';
  return (
    <button type="button" className={`${base} ${style}`} onClick={onClick}>
      {label}
    </button>
  );
}

function Results({ payload, onPreview, selectedChunkId, previewOpen }: {
  payload: SearchResponse;
  onPreview: (hit: SearchHit, trigger: HTMLButtonElement) => void;
  selectedChunkId: number | null;
  previewOpen: boolean;
}) {
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        {payload.retrieval?.mode === 'filters' ? `筛选命中 ${payload.total} 条` : payload.retrieval?.vector_used ? `本次召回 ${payload.total} 个候选` : `关键词命中 ${payload.total} 条（可浏览前 50 个候选）`} ·{' '}
        {payload.retrieval?.mode === 'filters'
          ? '按更新时间展示筛选结果'
          : payload.retrieval?.vector_used
            ? '关键词 + 向量混合排序'
            : '关键词排序'}{' '}
        ·
        显示第 {payload.offset + 1}–{payload.offset + payload.hits.length} 条
      </p>
      {payload.retrieval?.mode !== 'filters' && <p className="text-xs text-slate-500">关键词与混合检索使用有界候选，候选数不是全库统计；需要完整浏览时请清空关键词，使用分类、标签或来源筛选。</p>}
      {payload.retrieval?.degraded_reason && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          {payload.retrieval.degraded_reason}
        </p>
      )}
      <ol className="space-y-3">
        {payload.hits.map((hit) => (
          <li
            key={hit.chunk.id}
            className={`rounded-xl border bg-white p-4 shadow-sm ${previewOpen && selectedChunkId === hit.chunk.id ? 'border-cyan-500 ring-1 ring-cyan-500' : 'border-slate-200'}`}
          >
            <ResultHeader hit={hit} onPreview={onPreview} selected={previewOpen && selectedChunkId === hit.chunk.id} />
            <ResultSnippet hit={hit} />
            <ResultMeta hit={hit} />
            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
              <button type="button" onClick={(event) => onPreview(hit, event.currentTarget)} aria-controls="knowledge-preview" aria-expanded={previewOpen && selectedChunkId === hit.chunk.id} className="font-medium text-cyan-800 hover:underline">{previewOpen && selectedChunkId === hit.chunk.id ? '正在阅读' : '阅读原文 →'}</button>
              <Link href={searchEvidenceHref(hit.document_id, hit.chunk)} className="text-slate-500 hover:underline">完整文档</Link>
              <Link href={`/ask?document_ids=${hit.document_id}`} className="text-slate-500 hover:underline">仅问这份资料</Link>
            </div>
            {hit.supporting_evidence?.slice(0, 1).map((evidence) => (
              <details key={evidence.chunk.id} className="mt-3 border-t border-slate-100 pt-3">
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  同文档的补充证据
                  {evidence.chunk.page ? ` · 第 ${evidence.chunk.page} 页` : ''}
                </summary>
                <p className="mt-2 text-xs text-slate-500">
                  独立原文片段，与上方预览不一定连续。
                </p>
                <p className="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">
                  {evidence.context || evidence.snippet}
                </p>
                <button type="button" onClick={(event) => onPreview({ ...hit, document_id: evidence.document_id, document_version_id: evidence.document_version_id, chunk: evidence.chunk, snippet: evidence.snippet }, event.currentTarget)} aria-controls="knowledge-preview" className="mt-2 inline-block text-sm text-cyan-800 underline">在阅读区核对补充原文</button>
              </details>
            ))}
          </li>
        ))}
      </ol>
    </div>
  );
}

function ResultHeader({ hit, onPreview, selected }: { hit: SearchHit; onPreview: (hit: SearchHit, trigger: HTMLButtonElement) => void; selected: boolean }) {
  const headingLabel = hit.chunk.heading_path.length
    ? hit.chunk.heading_path.join(' › ')
    : null;
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <button
        type="button"
        onClick={(event) => onPreview(hit, event.currentTarget)}
        aria-controls="knowledge-preview"
        aria-expanded={selected}
        className="break-words text-left text-base font-semibold text-slate-900 hover:text-cyan-800 hover:underline"
      >
        {hit.title}
      </button>
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">
        {sourceTypeLabel(hit.source_type)}
      </span>
      {headingLabel && (
        <span className="text-xs text-slate-500">章节：{headingLabel}</span>
      )}
    </div>
  );
}

function ResultSnippet({ hit }: { hit: SearchHit }) {
  return (
    <p className="mt-3 text-sm leading-6 text-slate-700">
      {renderSnippet(hit.snippet, hit.highlights)}
    </p>
  );
}

function ResultMeta({ hit }: { hit: SearchHit }) {
  const meta: string[] = [];
  if (hit.chunk.page) meta.push(`第 ${hit.chunk.page} 页`);
  if (hit.chunk.paragraph_index !== null)
    meta.push(`段落 ${hit.chunk.paragraph_index + 1}`);
  if (hit.chunk.type) meta.push(`片段类型：${chunkTypeLabel(hit.chunk.type)}`);
  if (hit.categories.length > 0) {
    meta.push(`分类：${hit.categories.map((c) => c.name).join('、')}`);
  }
  if (hit.tags.length > 0) {
    meta.push(`标签：${hit.tags.map((t) => `#${t.name}`).join(' ')}`);
  }
  if (meta.length === 0) return null;
  return <p className="mt-3 text-xs text-slate-500">{meta.join(' · ')}</p>;
}

function renderSnippet(snippet: string, highlights: Highlight[]) {
  if (!highlights || highlights.length === 0) {
    return snippet;
  }
  const sorted = [...highlights].sort((a, b) => a.start - b.start);
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  sorted.forEach((range, index) => {
    if (range.start > cursor) {
      parts.push(snippet.slice(cursor, range.start));
    }
    parts.push(
      <mark
        key={`hl-${index}`}
        className="rounded bg-amber-100 px-0.5 text-amber-900"
      >
        {snippet.slice(range.start, range.end)}
      </mark>,
    );
    cursor = range.end;
  });
  if (cursor < snippet.length) {
    parts.push(snippet.slice(cursor));
  }
  return <>{parts}</>;
}

function EmptyState({
  heading,
  description,
}: {
  heading: string;
  description: string;
}) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <p className="text-lg font-medium text-slate-800">{heading}</p>
      <p className="mt-2 text-sm text-slate-500">{description}</p>
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <p>搜索失败：{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded border border-red-300 px-3 py-1 text-xs text-red-800 hover:bg-red-100"
      >
        重试
      </button>
    </div>
  );
}

const sourceTypeLabels: Record<string, string> = {
  note: '随手记',
  file: '文件',
  url: '链接',
};

function sourceTypeLabel(value: string) {
  return sourceTypeLabels[value] ?? value;
}

const chunkTypeLabels: Record<string, string> = {
  section: '章节',
  paragraph: '段落',
  list: '列表',
  code: '代码',
  quote: '引用',
  table: '表格',
  mixed: '混合',
};

function chunkTypeLabel(value: string) {
  return chunkTypeLabels[value] ?? value;
}
