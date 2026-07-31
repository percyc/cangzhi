'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

type Citation = {
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
};

type AskResponse = {
  question: string;
  answer: string;
  insufficient_evidence: boolean;
  citations: Citation[];
  evidence: Citation[];
  provider: string;
  model: string | null;
  retrieval?: {
    vector_used: boolean;
    degraded_reason: string | null;
  };
  scope?: { slug: string };
};

type KnowledgeScope = {
  id: number | null;
  slug: string;
  name: string;
  description: string | null;
  system: boolean;
  filter?: {
    category_ids?: number[];
    tag_ids?: number[];
    source_types?: string[];
    connector_ids?: number[];
    document_ids?: number[];
  };
};

type FacetCatalog = {
  categories: Array<{
    id: number;
    slug: string;
    name: string;
    parent_id: number | null;
    document_count: number;
  }>;
  tags: Array<{
    id: number;
    slug: string;
    name: string;
    document_count: number;
  }>;
  source_types: Array<{
    value: string;
    label: string;
    document_count: number;
  }>;
  connectors: Array<{
    id: number;
    name: string;
    is_enabled: boolean;
    document_count: number;
  }>;
};

type Turn = {
  id: number;
  question: string;
  response: AskResponse;
  contextLabel: string;
};

const ASK_SESSION_KEY = 'cangzhi:ask-session:v1';

export default function AskPage() {
  return (
    <Suspense fallback={<AskSkeleton />}>
      <AskClient />
    </Suspense>
  );
}

function AskSkeleton() {
  return (
    <main className="mx-auto max-w-6xl px-5 py-8">
      <div className="h-8 w-48 animate-pulse rounded bg-slate-200" />
      <div className="mt-6 h-80 animate-pulse rounded-3xl bg-slate-100" />
    </main>
  );
}

function AskClient() {
  const searchParams = useSearchParams();
  const [question, setQuestion] = useState(searchParams.get('q') ?? '');
  const [scopeSlug, setScopeSlug] = useState('all');
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [facets, setFacets] = useState<FacetCatalog | null>(null);
  const [categoryIds, setCategoryIds] = useState<number[]>([]);
  const [tagIds, setTagIds] = useState<number[]>([]);
  const [sourceTypes, setSourceTypes] = useState<string[]>([]);
  const [connectorIds, setConnectorIds] = useState<number[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providerReady, setProviderReady] = useState<boolean | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const saved = window.sessionStorage.getItem(ASK_SESSION_KEY);
        if (saved) {
          const value = JSON.parse(saved) as {
            turns?: Turn[];
            scopeSlug?: string;
            categoryIds?: number[];
            tagIds?: number[];
            sourceTypes?: string[];
            connectorIds?: number[];
            question?: string;
          };
          if (Array.isArray(value.turns)) setTurns(value.turns.slice(-20));
          if (typeof value.scopeSlug === 'string') setScopeSlug(value.scopeSlug);
          if (Array.isArray(value.categoryIds)) setCategoryIds(value.categoryIds);
          if (Array.isArray(value.tagIds)) setTagIds(value.tagIds);
          if (Array.isArray(value.sourceTypes)) setSourceTypes(value.sourceTypes);
          if (Array.isArray(value.connectorIds))
            setConnectorIds(value.connectorIds);
          if (!searchParams.get('q') && typeof value.question === 'string') {
            setQuestion(value.question);
          }
        }
      } catch {
        window.sessionStorage.removeItem(ASK_SESSION_KEY);
      } finally {
        setSessionReady(true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [searchParams]);

  useEffect(() => {
    if (!sessionReady) return;
    window.sessionStorage.setItem(
      ASK_SESSION_KEY,
      JSON.stringify({
        turns: turns.slice(-20),
        scopeSlug,
        categoryIds,
        tagIds,
        sourceTypes,
        connectorIds,
        question,
      }),
    );
  }, [
    turns,
    scopeSlug,
    categoryIds,
    tagIds,
    sourceTypes,
    connectorIds,
    question,
    sessionReady,
  ]);

  useEffect(() => {
    Promise.all([
      fetch('/api/v1/knowledge/scopes', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('知识范围加载失败');
          return (await response.json()) as { items: KnowledgeScope[] };
        },
      ),
      fetch('/api/ask/status', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('模型状态加载失败');
          return (await response.json()) as { provider_configured: boolean };
        },
      ),
      fetch('/api/v1/knowledge/facets', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('知识筛选项加载失败');
          return (await response.json()) as FacetCatalog;
        },
      ),
    ])
      .then(([scopeResult, status, facetResult]) => {
        setScopes(scopeResult.items);
        setProviderReady(status.provider_configured);
        setFacets(facetResult);
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : '初始化失败');
      });
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [turns, loading]);

  const runAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setError(null);
    const contextLabel = buildContextLabel(
      selectedScope?.name ?? '全部知识',
      facets,
      categoryIds,
      tagIds,
      sourceTypes,
      connectorIds,
    );
    try {
      const response = await fetch('/api/v1/knowledge/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: trimmed,
          scope_slug: scopeSlug,
          category_ids: categoryIds,
          tag_ids: tagIds,
          source_types: sourceTypes,
          connector_ids: connectorIds,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = body?.detail;
        throw new Error(
          typeof detail === 'object'
            ? (detail.message ?? '问答请求失败')
            : (detail ?? '问答请求失败'),
        );
      }
      setTurns((current) => [
        ...current,
        {
          id: Date.now(),
          question: trimmed,
          response: body as AskResponse,
          contextLabel,
        },
      ]);
      setQuestion('');
      window.history.replaceState(null, '', '/ask');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '问答失败');
    } finally {
      setLoading(false);
    }
  };

  const selectedScope = scopes.find((item) => item.slug === scopeSlug);
  const refinementCount =
    categoryIds.length +
    tagIds.length +
    sourceTypes.length +
    connectorIds.length;
  const clearRefinements = () => {
    setCategoryIds([]);
    setTagIds([]);
    setSourceTypes([]);
    setConnectorIds([]);
  };
  const changeScope = (slug: string) => {
    setScopeSlug(slug);
    clearRefinements();
  };

  return (
    <main className="mx-auto flex h-[calc(100dvh-4rem)] max-w-7xl gap-5 overflow-hidden px-3 py-3 sm:px-4 lg:px-6">
      <aside className="hidden w-64 shrink-0 lg:block">
        <div className="sticky top-5 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
            当前知识范围
          </p>
          <div className="mt-3 space-y-1">
            {scopes.map((scope) => (
              <button
                key={`${scope.system ? 'system' : 'saved'}-${scope.slug}`}
                type="button"
                onClick={() => changeScope(scope.slug)}
                className={`w-full rounded-xl px-3 py-2 text-left text-sm transition ${
                  scopeSlug === scope.slug
                    ? 'bg-slate-900 text-white'
                    : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {scope.name}
              </button>
            ))}
          </div>
          <p className="mt-4 border-t border-slate-100 pt-4 text-xs leading-5 text-slate-500">
            每轮都会重新检索所选范围，并保留可核验出处。当前会话只在本页保留，
            不会影响知识原文。
          </p>
          <Link
            href="/search"
            className="mt-4 block text-xs font-medium text-slate-700 hover:text-slate-950"
          >
            切换到精确搜索 →
          </Link>
        </div>
      </aside>

      <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-7">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-950">
              问知识库
            </h1>
            <p className="mt-1 text-xs text-slate-500">
              {selectedScope?.name ?? '全部知识'} · 检索证据后回答
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={scopeSlug}
              onChange={(event) => changeScope(event.target.value)}
              className="max-w-48 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 lg:hidden"
              aria-label="知识范围"
            >
              {scopes.map((scope) => (
                <option key={scope.slug} value={scope.slug}>
                  {scope.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setFilterOpen((current) => !current)}
              className={`rounded-xl border px-3 py-2 text-xs font-medium ${
                refinementCount > 0
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : 'border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}
            >
              细化范围{refinementCount > 0 ? ` · ${refinementCount}` : ''}
            </button>
            {turns.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  setTurns([]);
                  setError(null);
                }}
                className="rounded-xl border border-slate-200 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50"
              >
                新对话
              </button>
            )}
          </div>
        </header>

        {filterOpen && (
          <FacetPanel
            facets={facets}
            selectedScope={selectedScope}
            categoryIds={categoryIds}
            tagIds={tagIds}
            sourceTypes={sourceTypes}
            connectorIds={connectorIds}
            setCategoryIds={setCategoryIds}
            setTagIds={setTagIds}
            setSourceTypes={setSourceTypes}
            setConnectorIds={setConnectorIds}
            onClear={clearRefinements}
          />
        )}

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-slate-50/60 px-4 py-6 sm:px-7">
          {turns.length === 0 && !loading && (
            <WelcomeState
              disabled={providerReady === false}
              onExample={setQuestion}
            />
          )}
          <div className="space-y-8">
            {turns.map((turn) => (
              <ConversationTurn key={turn.id} turn={turn} />
            ))}
            {loading && (
              <div className="flex gap-3">
                <AssistantMark />
                <div className="rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 shadow-sm">
                  正在检索、核对出处并组织回答…
                </div>
              </div>
            )}
          </div>
          <div ref={endRef} />
        </div>

        <footer className="shrink-0 border-t border-slate-100 bg-white p-3 sm:px-7 sm:py-4">
          {providerReady === false && (
            <p className="mb-3 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-800">
              尚未配置对话模型。
              <Link href="/settings" className="ml-1 font-medium underline">
                前往设置
              </Link>
            </p>
          )}
          {error && (
            <p className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </p>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void runAsk();
            }}
            className="flex items-end gap-3 rounded-2xl border border-slate-200 bg-slate-50 p-2 focus-within:border-slate-400 focus-within:bg-white"
          >
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === 'Enter' &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  void runAsk();
                }
              }}
              placeholder="询问你保存过的资料…"
              rows={2}
              maxLength={500}
              className="max-h-40 min-h-12 flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-6 text-slate-900 outline-none placeholder:text-slate-400"
            />
            <button
              type="submit"
              disabled={!question.trim() || loading || providerReady === false}
              className="rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              发送
            </button>
          </form>
          <p className="mt-2 text-center text-[11px] text-slate-400">
            Enter 发送 · Shift + Enter 换行 · 回答可能有误，请核对引用原文
          </p>
        </footer>
      </section>
    </main>
  );
}

function WelcomeState({
  disabled,
  onExample,
}: {
  disabled: boolean;
  onExample: (value: string) => void;
}) {
  const examples = [
    '总结我收藏过的向量检索方案',
    '我保存的资料里，如何避免知识切片截断？',
    '比较几篇资料对个人知识管理的不同观点',
  ];
  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center py-16 text-center">
      <AssistantMark large />
      <h2 className="mt-5 text-2xl font-semibold text-slate-900">
        从自己的知识出发
      </h2>
      <p className="mt-2 max-w-lg text-sm leading-6 text-slate-500">
        藏知会在当前范围中同时使用关键词和可用的向量索引检索，并把答案链接回原文。
      </p>
      <div className="mt-7 grid w-full gap-2 sm:grid-cols-3">
        {examples.map((example) => (
          <button
            key={example}
            type="button"
            disabled={disabled}
            onClick={() => onExample(example)}
            className="rounded-2xl border border-slate-200 bg-white p-3 text-left text-xs leading-5 text-slate-600 shadow-sm hover:border-slate-300 hover:text-slate-900 disabled:opacity-50"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}

function FacetPanel({
  facets,
  selectedScope,
  categoryIds,
  tagIds,
  sourceTypes,
  connectorIds,
  setCategoryIds,
  setTagIds,
  setSourceTypes,
  setConnectorIds,
  onClear,
}: {
  facets: FacetCatalog | null;
  selectedScope: KnowledgeScope | undefined;
  categoryIds: number[];
  tagIds: number[];
  sourceTypes: string[];
  connectorIds: number[];
  setCategoryIds: (value: number[]) => void;
  setTagIds: (value: number[]) => void;
  setSourceTypes: (value: string[]) => void;
  setConnectorIds: (value: number[]) => void;
  onClear: () => void;
}) {
  const [tagQuery, setTagQuery] = useState('');
  const count =
    categoryIds.length +
    tagIds.length +
    sourceTypes.length +
    connectorIds.length;
  if (!facets) {
    return (
      <div className="border-b border-slate-100 bg-slate-50 px-5 py-4 text-xs text-slate-500 sm:px-7">
        正在加载知识筛选项…
      </div>
    );
  }
  const constrainedSources = selectedScope?.filter?.source_types ?? [];
  const visibleTags = facets.tags
    .filter(
      (tag) =>
        tag.document_count > 0 &&
        (tagIds.includes(tag.id) ||
          !tagQuery.trim() ||
          `${tag.name} ${tag.slug}`
            .toLowerCase()
            .includes(tagQuery.trim().toLowerCase())),
    )
    .sort(
      (left, right) =>
        Number(tagIds.includes(right.id)) - Number(tagIds.includes(left.id)),
    )
    .slice(0, 24);
  return (
    <div className="border-b border-slate-100 bg-slate-50/80 px-5 py-5 sm:px-7">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">细化当前知识范围</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            同一组满足任意一项，不同组需要同时满足；这些条件只会缩小
            “{selectedScope?.name ?? '全部知识'}”。
          </p>
        </div>
        {count > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="text-xs font-medium text-slate-600 hover:text-slate-950"
          >
            清除全部
          </button>
        )}
      </div>

      <FacetGroup title="分类">
        {facets.categories
          .filter((item) => item.document_count > 0)
          .map((item) => (
            <FacetChip
              key={item.id}
              label={`${item.name}（${item.document_count}）`}
              active={categoryIds.includes(item.id)}
              onClick={() =>
                setCategoryIds(toggleValue(categoryIds, item.id))
              }
            />
          ))}
      </FacetGroup>

      <FacetGroup title="标签">
        <input
          value={tagQuery}
          onChange={(event) => setTagQuery(event.target.value)}
          placeholder="搜索标签"
          className="mb-2 w-full max-w-xs rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs outline-none focus:border-slate-400"
        />
        <div className="flex flex-wrap gap-2">
          {visibleTags.map((item) => (
            <FacetChip
              key={item.id}
              label={`#${item.name}（${item.document_count}）`}
              active={tagIds.includes(item.id)}
              onClick={() => setTagIds(toggleValue(tagIds, item.id))}
            />
          ))}
          {visibleTags.length === 0 && (
            <span className="text-xs text-slate-400">没有匹配的可用标签</span>
          )}
        </div>
      </FacetGroup>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <FacetGroup title="来源类型" compact>
          {constrainedSources.length > 0 ? (
            <p className="text-xs text-slate-500">
              已由基础范围限定为：
              {facets.source_types
                .filter((item) => constrainedSources.includes(item.value))
                .map((item) => item.label)
                .join('、')}
            </p>
          ) : (
            facets.source_types
              .filter((item) => item.document_count > 0)
              .map((item) => (
                <FacetChip
                  key={item.value}
                  label={`${item.label}（${item.document_count}）`}
                  active={sourceTypes.includes(item.value)}
                  onClick={() =>
                    setSourceTypes(toggleValue(sourceTypes, item.value))
                  }
                />
              ))
          )}
        </FacetGroup>
        <FacetGroup title="WebDAV 连接器" compact>
          {facets.connectors.filter((item) => item.document_count > 0).length >
          0 ? (
            facets.connectors
              .filter((item) => item.document_count > 0)
              .map((item) => (
                <FacetChip
                  key={item.id}
                  label={`${item.name}（${item.document_count}）`}
                  active={connectorIds.includes(item.id)}
                  onClick={() =>
                    setConnectorIds(toggleValue(connectorIds, item.id))
                  }
                />
              ))
          ) : (
            <span className="text-xs text-slate-400">暂无已入库的连接器资料</span>
          )}
        </FacetGroup>
      </div>
    </div>
  );
}

function FacetGroup({
  title,
  compact = false,
  children,
}: {
  title: string;
  compact?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={compact ? '' : 'mt-4'}>
      <h3 className="mb-2 text-xs font-semibold text-slate-600">{title}</h3>
      <div className="flex flex-wrap gap-2">{children}</div>
    </section>
  );
}

function FacetChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active
          ? 'border-slate-900 bg-slate-900 text-white'
          : 'border-slate-200 bg-white text-slate-600 hover:border-slate-400'
      }`}
    >
      {label}
    </button>
  );
}

function toggleValue<T>(values: T[], value: T): T[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

function buildContextLabel(
  scopeName: string,
  facets: FacetCatalog | null,
  categoryIds: number[],
  tagIds: number[],
  sourceTypes: string[],
  connectorIds: number[],
) {
  if (!facets) return scopeName;
  const labels = [
    ...facets.categories
      .filter((item) => categoryIds.includes(item.id))
      .map((item) => item.name),
    ...facets.tags
      .filter((item) => tagIds.includes(item.id))
      .map((item) => `#${item.name}`),
    ...facets.source_types
      .filter((item) => sourceTypes.includes(item.value))
      .map((item) => item.label),
    ...facets.connectors
      .filter((item) => connectorIds.includes(item.id))
      .map((item) => item.name),
  ];
  return [scopeName, ...labels].join(' · ');
}

function ConversationTurn({ turn }: { turn: Turn }) {
  return (
    <article className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-slate-900 px-4 py-3 text-sm leading-6 text-white">
          {turn.question}
        </div>
      </div>
      <div className="flex items-start gap-3">
        <AssistantMark />
        <div className="min-w-0 max-w-3xl flex-1">
          <div className="prose prose-slate max-w-none rounded-2xl rounded-tl-md border border-slate-200 bg-white px-5 py-4 text-sm leading-7 shadow-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>
              {turn.response.answer}
            </ReactMarkdown>
          </div>
          <ResultMeta response={turn.response} contextLabel={turn.contextLabel} />
          {turn.response.citations.length > 0 && (
            <CitationList citations={turn.response.citations} />
          )}
        </div>
      </div>
    </article>
  );
}

function ResultMeta({
  response,
  contextLabel,
}: {
  response: AskResponse;
  contextLabel: string;
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-400">
      <span>范围：{contextLabel}</span>
      <span>
        {response.retrieval?.vector_used ? '混合检索' : '关键词检索'}
      </span>
      {response.provider && (
        <span>
          {response.provider}
          {response.model ? ` · ${response.model}` : ''}
        </span>
      )}
      {response.insufficient_evidence && (
        <span className="text-amber-700">证据不足</span>
      )}
      {response.retrieval?.degraded_reason && (
        <span className="text-amber-700">
          {response.retrieval.degraded_reason}
        </span>
      )}
    </div>
  );
}

function CitationList({ citations }: { citations: Citation[] }) {
  return (
    <details className="mt-3 rounded-2xl border border-slate-200 bg-white">
      <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-slate-600">
        查看 {citations.length} 条引用
      </summary>
      <ol className="border-t border-slate-100">
        {citations.map((citation, index) => (
          <CitationItem
            key={`${citation.chunk_id}-${citation.id}`}
            citation={citation}
            index={index}
          />
        ))}
      </ol>
    </details>
  );
}

function CitationItem({
  citation,
  index,
}: {
  citation: Citation;
  index: number;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [contentLabel, setContentLabel] = useState('完整引用片段');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadFullChunk = async () => {
    if (content !== null || loading) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch(
        `/api/v1/knowledge/chunks/${citation.chunk_id}`,
        { cache: 'no-store' },
      );
      if (!response.ok) throw new Error('完整引用读取失败');
      const body = (await response.json()) as {
        content?: string;
        parent_id?: number | null;
      };
      if (body.parent_id) {
        const parentResponse = await fetch(
          `/api/v1/knowledge/chunks/${body.parent_id}`,
          { cache: 'no-store' },
        );
        if (parentResponse.ok) {
          const parent = (await parentResponse.json()) as { content?: string };
          if (parent.content) {
            setContentLabel('完整引用所在章节');
            setContent(parent.content);
            return;
          }
        }
      }
      setContent(body.content || citation.snippet);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '完整引用读取失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <li className="border-b border-slate-100 p-4 last:border-b-0">
      <details
        onToggle={(event) => {
          if (event.currentTarget.open) void loadFullChunk();
        }}
      >
        <summary className="cursor-pointer list-none">
          <div className="flex items-start gap-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-600">
              {index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-semibold text-slate-800">
                  {citation.title}
                </span>
                <span className="shrink-0 text-[11px] font-medium text-slate-500">
                  展开引用
                </span>
              </div>
              <p className="mt-1 line-clamp-3 text-xs leading-5 text-slate-500">
                {citation.snippet}
              </p>
              <p className="mt-2 text-[11px] text-slate-400">
                {citation.heading_path.length > 0
                  ? citation.heading_path.join(' › ')
                  : sourceTypeLabel(citation.source_type)}
                {citation.page ? ` · 第 ${citation.page} 页` : ''}
              </p>
            </div>
          </div>
        </summary>
        <div className="ml-9 mt-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
          {loading && (
            <p className="text-xs text-slate-500">正在读取完整引用片段…</p>
          )}
          {error && <p className="text-xs text-red-700">{error}</p>}
          {content !== null && (
            <>
              <p className="mb-2 text-[11px] font-semibold text-slate-500">
                {contentLabel}
              </p>
              <p className="whitespace-pre-wrap text-sm leading-6 text-slate-700">
                {content}
              </p>
            </>
          )}
          <Link
            href={`/documents/${citation.document_id}?return_to=ask`}
            className="mt-3 inline-flex text-xs font-medium text-blue-700 hover:underline"
          >
            打开完整文档 →
          </Link>
        </div>
      </details>
    </li>
  );
}

function AssistantMark({ large = false }: { large?: boolean }) {
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-slate-800 to-slate-950 font-semibold text-white shadow-sm ${
        large ? 'h-12 w-12 text-lg' : 'h-8 w-8 text-xs'
      }`}
      aria-hidden="true"
    >
      知
    </span>
  );
}

function sourceTypeLabel(value: string) {
  return (
    {
      note: '随手记',
      file: '文件',
      url: '网页',
    }[value] ?? value
  );
}
