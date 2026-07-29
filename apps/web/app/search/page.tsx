'use client';

import { Suspense, useEffect, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';

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

  const [query, setQuery] = useState(() => searchParams.get('q') ?? '');
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  const [categorySlugs, setCategorySlugs] = useState<string[]>([]);
  const [tagSlugs, setTagSlugs] = useState<string[]>([]);
  const [sourceTypes, setSourceTypes] = useState<string[]>([]);
  const [filters, setFilters] = useState<FiltersPayload | null>(null);
  const [filtersError, setFiltersError] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: 'idle' });
  const [retryNonce, setRetryNonce] = useState(0);

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
      setDebouncedQuery(trimmed);
      if (!trimmed) {
        setState({ kind: 'idle' });
      }
    }, 250);
    return () => window.clearTimeout(handle);
  }, [query]);

  useEffect(() => {
    if (!debouncedQuery) {
      return;
    }
    let cancelled = false;
    const run = async () => {
      setState({ kind: 'loading' });
      try {
        const response = await fetch('/api/search', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: debouncedQuery,
            limit: 20,
            offset: 0,
            category_slugs: categorySlugs,
            tag_slugs: tagSlugs,
            source_types: sourceTypes,
          }),
        });
        if (cancelled) {
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || '搜索请求失败');
        }
        const payload = (await response.json()) as SearchResponse;
        if (cancelled) {
          return;
        }
        setState({ kind: 'ready', payload });
        const params = new URLSearchParams();
        params.set('q', debouncedQuery);
        window.history.replaceState(null, '', `/search?${params.toString()}`);
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
    };
  }, [debouncedQuery, categorySlugs, tagSlugs, sourceTypes, retryNonce]);

  const toggleCategory = (slug: string) => {
    setCategorySlugs((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  };
  const toggleTag = (slug: string) => {
    setTagSlugs((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  };
  const toggleSourceType = (value: string) => {
    setSourceTypes((prev) =>
      prev.includes(value) ? prev.filter((s) => s !== value) : [...prev, value],
    );
  };

  const hasAnyFilter =
    categorySlugs.length > 0 || tagSlugs.length > 0 || sourceTypes.length > 0;

  return (
    <main className="container mx-auto max-w-5xl p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">
        ← 返回资料列表
      </Link>
      <h1 className="mt-4 text-2xl font-bold text-slate-900">搜索</h1>
      <p className="mt-1 text-sm text-slate-500">
        用关键词找到之前保存的资料。每个结果会显示命中片段、章节路径以及定位信息，点击可进入资料详情。
      </p>

      <form
        className="mt-6"
        onSubmit={(event) => {
          event.preventDefault();
          setDebouncedQuery(query.trim());
          setRetryNonce((value) => value + 1);
        }}
      >
        <label htmlFor="q" className="sr-only">
          搜索关键词
        </label>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
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
        {!debouncedQuery && (
          <EmptyState
            heading="开始一次搜索"
            description="支持中文、英文和数字。可以同时选择分类、标签、来源类型来缩小范围。"
          />
        )}
        {debouncedQuery && state.kind === 'loading' && (
          <p className="text-sm text-slate-500">正在搜索…</p>
        )}
        {debouncedQuery && state.kind === 'error' && (
          <ErrorState
            message={state.message}
            onRetry={() => setRetryNonce((value) => value + 1)}
          />
        )}
        {debouncedQuery && state.kind === 'ready' &&
          (state.payload.hits.length === 0 ? (
            <EmptyState
              heading="没有找到匹配的资料"
              description="试试调整关键词、清空筛选，或确认相关资料已经被成功解析。"
            />
          ) : (
            <Results payload={state.payload} />
          ))}
      </section>
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
    <div className="mt-5 grid gap-4 md:grid-cols-3">
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

function Results({ payload }: { payload: SearchResponse }) {
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        命中 {payload.total} 条 ·{' '}
        {payload.retrieval?.vector_used ? '关键词 + 向量混合排序' : '关键词排序'} ·
        显示 {payload.hits.length} 条
      </p>
      {payload.retrieval?.degraded_reason && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          {payload.retrieval.degraded_reason}
        </p>
      )}
      <ol className="space-y-3">
        {payload.hits.map((hit) => (
          <li
            key={hit.chunk.id}
            className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
          >
            <ResultHeader hit={hit} />
            <ResultSnippet hit={hit} />
            <ResultMeta hit={hit} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function ResultHeader({ hit }: { hit: SearchHit }) {
  const headingLabel = hit.chunk.heading_path.length
    ? hit.chunk.heading_path.join(' › ')
    : null;
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <Link
        href={`/documents/${hit.document_id}`}
        className="text-base font-semibold text-slate-900 hover:underline"
      >
        {hit.title}
      </Link>
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
