'use client';

import { Suspense, useEffect, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';

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

type AskCitation = {
  id: number;
  document_id: number;
  document_version_id: number;
  chunk_id: number;
  title: string;
  heading_path: string[];
  page: number | null;
  paragraph_index: number | null;
  source_start: number | null;
  source_end: number | null;
  source_type: string;
  source_url: string | null;
  snippet: string;
  categories: { id: number; slug: string; name: string }[];
  tags: { id: number; slug: string; name: string }[];
};

type AskResponse = {
  question: string;
  answer: string;
  insufficient_evidence: boolean;
  citations: AskCitation[];
  evidence: AskCitation[];
  provider: string;
  model: string | null;
};

type AskStatus = {
  provider_configured: boolean;
  provider: string;
};

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; payload: AskResponse };

export default function AskPage() {
  return (
    <Suspense fallback={<AskSkeleton />}>
      <AskClient />
    </Suspense>
  );
}

function AskSkeleton() {
  return (
    <main className="container mx-auto max-w-4xl p-4">
      <p className="text-sm text-slate-500">加载中…</p>
    </main>
  );
}

function AskClient() {
  const searchParams = useSearchParams();
  const initialQuestion = searchParams.get('q') ?? '';

  const [question, setQuestion] = useState(initialQuestion);
  const [categorySlugs, setCategorySlugs] = useState<string[]>([]);
  const [tagSlugs, setTagSlugs] = useState<string[]>([]);
  const [sourceTypes, setSourceTypes] = useState<string[]>([]);
  const [filters, setFilters] = useState<FiltersPayload | null>(null);
  const [filtersError, setFiltersError] = useState<string | null>(null);
  const [status, setStatus] = useState<AskStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [state, setState] = useState<State>({ kind: 'idle' });

  useEffect(() => {
    fetch('/api/ask/status', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error('暂时无法读取问答状态');
        return res.json();
      })
      .then((data: AskStatus) => {
        setStatus(data);
        setStatusError(null);
      })
      .catch((err) => {
        setStatusError(err instanceof Error ? err.message : '读取问答状态失败');
      });
  }, []);

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

  const runAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || state.kind === 'loading') return;
    setState({ kind: 'loading' });
    try {
      const response = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: trimmed,
          category_slugs: categorySlugs,
          tag_slugs: tagSlugs,
          source_types: sourceTypes,
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const detail = body?.detail;
        if (detail && typeof detail === 'object') {
          throw new Error(detail.message ?? '问答请求失败');
        }
        throw new Error(typeof detail === 'string' ? detail : '问答请求失败');
      }
      const payload = (await response.json()) as AskResponse;
      setState({ kind: 'ready', payload });
      const params = new URLSearchParams();
      params.set('q', trimmed);
      window.history.replaceState(null, '', `/ask?${params.toString()}`);
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof Error ? err.message : '问答失败',
      });
    }
  };

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
    <main className="container mx-auto max-w-4xl p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">
        ← 返回资料列表
      </Link>
      <h1 className="mt-4 text-2xl font-bold text-slate-900">问知识库</h1>
      <p className="mt-1 text-sm text-slate-500">
        用自然语言提问，系统会从你保存的资料中找到相关片段并给出带出处的中文回答。
        回答中的引用会跳回对应资料。
      </p>

      {status && !status.provider_configured && (
        <Notice kind="warn">
          当前没有配置问答模型，资料里的内容暂时无法用自然语言提问。
          <Link href="/settings" className="ml-2 text-amber-900 underline">
            前往模型设置
          </Link>
        </Notice>
      )}
      {statusError && <Notice kind="error">问答状态读取失败：{statusError}</Notice>}

      <form
        className="mt-6"
        onSubmit={(event) => {
          event.preventDefault();
          void runAsk();
        }}
      >
        <label htmlFor="question" className="sr-only">
          问题
        </label>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            id="question"
            type="text"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="例如：向量检索是怎么实现的？"
            className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
            autoFocus
            maxLength={500}
          />
          <button
            type="submit"
            className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:bg-slate-400"
            disabled={!question.trim() || state.kind === 'loading'}
          >
            提问
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
        {state.kind === 'idle' && (
          <EmptyState
            heading="开始一次提问"
            description="例如：“藏知支持哪些文件格式？”或“我之前学过的向量检索笔记说了什么？”可以同时选择分类、标签和来源类型来缩小检索范围。"
          />
        )}
        {state.kind === 'loading' && (
          <p className="text-sm text-slate-500">正在从知识库中寻找答案…</p>
        )}
        {state.kind === 'error' && (
          <ErrorState
            message={state.message}
            onRetry={() => void runAsk()}
          />
        )}
        {state.kind === 'ready' && <AskResult payload={state.payload} />}
      </section>
    </main>
  );
}

function Notice({
  kind,
  children,
}: {
  kind: 'warn' | 'error';
  children: React.ReactNode;
}) {
  const style =
    kind === 'warn'
      ? 'border-amber-200 bg-amber-50 text-amber-900'
      : 'border-red-200 bg-red-50 text-red-800';
  return (
    <div className={`mt-4 rounded-xl border p-3 text-sm ${style}`}>{children}</div>
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

function AskResult({ payload }: { payload: AskResponse }) {
  return (
    <div className="space-y-4">
      <article className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <p className="text-xs uppercase tracking-wide text-slate-500">回答</p>
        {payload.insufficient_evidence ? (
          <p className="mt-2 text-sm leading-7 text-slate-700">{payload.answer}</p>
        ) : (
          <p className="mt-2 whitespace-pre-line text-sm leading-7 text-slate-800">
            {payload.answer}
          </p>
        )}
        {payload.provider && !payload.insufficient_evidence && (
          <p className="mt-3 text-xs text-slate-400">
            模型：{payload.provider}
            {payload.model ? ` · ${payload.model}` : ''}
          </p>
        )}
      </article>

      {payload.citations.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-slate-700">
            引用 · {payload.citations.length} 条
          </h3>
          <ol className="mt-2 space-y-2">
            {payload.citations.map((citation) => (
              <li
                key={citation.id}
                className="rounded-xl border border-slate-200 bg-white p-3 text-sm shadow-sm"
              >
                <CitationHeader citation={citation} />
                <p className="mt-2 whitespace-pre-line text-slate-700">{citation.snippet}</p>
                <CitationMeta citation={citation} />
              </li>
            ))}
          </ol>
        </section>
      )}

      {payload.evidence.length > payload.citations.length && (
        <details className="rounded-xl border border-dashed border-slate-300 bg-white p-3 text-sm">
          <summary className="cursor-pointer text-slate-600">
            查看其他被检索到的资料（{payload.evidence.length - payload.citations.length}）
          </summary>
          <ol className="mt-2 space-y-2">
            {payload.evidence
              .filter((item) => !payload.citations.some((c) => c.id === item.id))
              .map((item) => (
                <li
                  key={item.id}
                  className="rounded border border-slate-100 bg-slate-50 p-2 text-xs text-slate-600"
                >
                  <Link
                    href={`/documents/${item.document_id}`}
                    className="font-semibold text-slate-800 hover:underline"
                  >
                    {item.title}
                  </Link>
                  <p className="mt-1 text-slate-600">{item.snippet}</p>
                </li>
              ))}
          </ol>
        </details>
      )}
    </div>
  );
}

function CitationHeader({ citation }: { citation: AskCitation }) {
  const headingLabel = citation.heading_path.length
    ? citation.heading_path.join(' › ')
    : null;
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <Link
        href={`/documents/${citation.document_id}`}
        className="text-base font-semibold text-slate-900 hover:underline"
      >
        {citation.title}
      </Link>
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">
        {sourceTypeLabel(citation.source_type)}
      </span>
      {headingLabel && (
        <span className="text-xs text-slate-500">章节：{headingLabel}</span>
      )}
    </div>
  );
}

function CitationMeta({ citation }: { citation: AskCitation }) {
  const meta: string[] = [];
  if (citation.page) meta.push(`第 ${citation.page} 页`);
  if (citation.paragraph_index !== null)
    meta.push(`段落 ${citation.paragraph_index + 1}`);
  if (meta.length === 0) return null;
  return <p className="mt-2 text-xs text-slate-500">{meta.join(' · ')}</p>;
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
      <p>问答失败：{message}</p>
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
