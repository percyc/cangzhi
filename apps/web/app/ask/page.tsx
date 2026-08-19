'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import {
  EvidenceDrawer,
  type EvidenceCitation,
} from '@/components/evidence-drawer';

type Citation = EvidenceCitation & {
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
  table_location?: {
    sheet_name?: string;
    region_index?: number;
    row_start?: number;
    row_end?: number;
    column_names?: string[];
  };
  source_type: string;
  source_url: string | null;
  snippet: string;
};

type AnalysisStep = {
  tool: string;
  label: string;
  status: string;
  result_count?: number;
  detail?: string;
};

type EvidenceAudit = {
  status: 'sufficient' | 'needs_more' | 'insufficient';
  summary: string;
  missing_evidence: string[];
  next_query?: string | null;
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
    mode?: string;
    structured_table?: {
      matched_rows: number;
      metric: string;
      metric_column?: string | null;
      group_by?: string[];
      warnings?: string[];
    };
    analysis?: {
      plan_summary?: string;
      iterations?: number;
      tool_calls: number;
      max_tool_calls: number;
      steps: AnalysisStep[];
      evidence_audits?: EvidenceAudit[];
    };
  };
  scope?: { slug: string };
  history?: { conversation_id: number; turn_id: number };
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
  mode: 'quick' | 'deep';
};

type ConversationSummary = {
  id: number;
  title: string;
  turn_count: number;
  last_asked_at: string | null;
  created_at: string;
};

type ConversationDetail = ConversationSummary & {
  memory_enabled: false;
  turns: Array<{
    id: number;
    question: string;
    mode: 'quick' | 'deep';
    context_label: string | null;
    response: AskResponse;
  }>;
};

type LiveAnalysis = {
  question: string;
  contextLabel: string;
  message: string;
  steps: AnalysisStep[];
  evidenceAudits: EvidenceAudit[];
  toolCalls: number;
  maxToolCalls: number;
  iterations?: number;
};

type AskStreamEvent =
  | {
      type: 'progress';
      phase: string;
      message?: string;
      step?: AnalysisStep;
      audit?: EvidenceAudit;
      tool_calls?: number;
      max_tool_calls?: number;
      iterations?: number;
    }
  | { type: 'heartbeat' }
  | { type: 'result'; data: AskResponse }
  | { type: 'error'; error: { code?: string; message?: string } };

const ASK_SESSION_KEY = 'cangzhi:ask-preferences:v2';

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
  const [mode, setMode] = useState<'quick' | 'deep'>('quick');
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
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [liveAnalysis, setLiveAnalysis] = useState<LiveAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [providerReady, setProviderReady] = useState<boolean | null>(null);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const requestAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      requestAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const saved = window.sessionStorage.getItem(ASK_SESSION_KEY);
        if (saved) {
          const value = JSON.parse(saved) as {
            scopeSlug?: string;
            categoryIds?: number[];
            tagIds?: number[];
            sourceTypes?: string[];
            connectorIds?: number[];
            question?: string;
            mode?: 'quick' | 'deep';
          };
          if (typeof value.scopeSlug === 'string') setScopeSlug(value.scopeSlug);
          if (Array.isArray(value.categoryIds)) setCategoryIds(value.categoryIds);
          if (Array.isArray(value.tagIds)) setTagIds(value.tagIds);
          if (Array.isArray(value.sourceTypes)) setSourceTypes(value.sourceTypes);
          if (Array.isArray(value.connectorIds))
            setConnectorIds(value.connectorIds);
          if (!searchParams.get('q') && typeof value.question === 'string') {
            setQuestion(value.question);
          }
          if (value.mode === 'quick' || value.mode === 'deep') setMode(value.mode);
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
        scopeSlug,
        categoryIds,
        tagIds,
        sourceTypes,
        connectorIds,
        question,
        mode,
      }),
    );
  }, [
    scopeSlug,
    categoryIds,
    tagIds,
    sourceTypes,
    connectorIds,
    question,
    mode,
    sessionReady,
  ]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch('/api/ask/conversations', { cache: 'no-store' });
        if (!response.ok) throw new Error('问答记录加载失败');
        const payload = (await response.json()) as { items: ConversationSummary[] };
        if (cancelled) return;
        setConversations(payload.items);
        if (!searchParams.get('q') && payload.items[0]) {
          await loadConversation(payload.items[0].id);
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '问答记录加载失败');
      }
    };
    void load();
    return () => { cancelled = true; };
  // Initial cloud restore only; URL search params are stable for this page mount.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
  }, [turns, loading, liveAnalysis]);

  const refreshConversations = async () => {
    const response = await fetch('/api/ask/conversations', { cache: 'no-store' });
    if (!response.ok) throw new Error('问答记录加载失败');
    const payload = (await response.json()) as { items: ConversationSummary[] };
    if (mountedRef.current) setConversations(payload.items);
  };

  async function loadConversation(id: number) {
    setHistoryLoading(true);
    try {
      const response = await fetch(`/api/ask/conversations/${id}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('问答记录读取失败');
      const payload = (await response.json()) as ConversationDetail;
      if (!mountedRef.current) return;
      setConversationId(payload.id);
      setTurns(payload.turns.map((turn) => ({
        id: turn.id,
        question: turn.question,
        response: turn.response,
        contextLabel: turn.context_label ?? '全部知识',
        mode: turn.mode,
      })));
      setHistoryOpen(false);
      setError(null);
      window.history.replaceState(null, '', '/ask');
    } finally {
      if (mountedRef.current) setHistoryLoading(false);
    }
  }

  const startNewConversation = () => {
    setConversationId(null);
    setTurns([]);
    setQuestion('');
    setError(null);
    setHistoryOpen(false);
    window.history.replaceState(null, '', '/ask');
  };

  const deleteConversation = async (id: number) => {
    const response = await fetch(`/api/ask/conversations/${id}`, { method: 'DELETE' });
    if (!response.ok) throw new Error('删除问答记录失败');
    if (conversationId === id) startNewConversation();
    await refreshConversations();
  };

  const runAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || loading || requestAbortRef.current) return;
    const controller = new AbortController();
    requestAbortRef.current = controller;
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
    if (mode === 'deep') {
      setLiveAnalysis({
        question: trimmed,
        contextLabel,
        message: '正在分析问题',
        steps: [],
        evidenceAudits: [],
        toolCalls: 0,
        maxToolCalls: 12,
      });
    }
    let targetConversationId = conversationId;
    let createdConversation = false;
    let answerSaved = false;
    try {
      if (targetConversationId === null) {
        const createdResponse = await fetch('/api/ask/conversations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: '新问答' }),
          signal: controller.signal,
        });
        if (!createdResponse.ok) throw new Error('无法创建问答记录');
        const created = (await createdResponse.json()) as ConversationSummary;
        targetConversationId = created.id;
        createdConversation = true;
        setConversationId(created.id);
      }
      const requestBody = {
        question: trimmed,
        mode,
        scope_slug: scopeSlug,
        category_ids: categoryIds,
        tag_ids: tagIds,
        source_types: sourceTypes,
        connector_ids: connectorIds,
        conversation_id: targetConversationId,
        context_label: contextLabel,
      };
      const response = await fetch(
        mode === 'deep'
          ? '/api/v1/knowledge/ask/stream'
          : '/api/v1/knowledge/ask',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const detail = body?.detail;
        throw new Error(
          typeof detail === 'object'
            ? (detail.message ?? '问答请求失败')
            : (detail ?? '问答请求失败'),
        );
      }
      const body =
        mode === 'deep'
          ? await readAskStream(response, (event) => {
              if (event.type !== 'progress') return;
              if (!mountedRef.current) return;
              setLiveAnalysis((current) => {
                if (!current) return current;
                const steps = event.step
                  ? [...current.steps, event.step]
                  : current.steps;
                const evidenceAudits = event.audit
                  ? [...current.evidenceAudits, event.audit]
                  : current.evidenceAudits;
                return {
                  ...current,
                  message:
                    event.message ??
                    (event.step
                      ? progressMessage(event.step)
                      : current.message),
                  steps,
                  evidenceAudits,
                  toolCalls: event.tool_calls ?? current.toolCalls,
                  maxToolCalls: event.max_tool_calls ?? current.maxToolCalls,
                  iterations: event.iterations ?? current.iterations,
                };
              });
            })
          : ((await response.json()) as AskResponse);
      setTurns((current) => [
        ...current,
        {
          id: body.history?.turn_id ?? Date.now(),
          question: trimmed,
          response: body as AskResponse,
          contextLabel,
          mode,
        },
      ]);
      setQuestion('');
      window.history.replaceState(null, '', '/ask');
      answerSaved = true;
      void refreshConversations().catch(() => undefined);
    } catch (reason) {
      if (createdConversation && !answerSaved && targetConversationId !== null) {
        await fetch(`/api/ask/conversations/${targetConversationId}`, { method: 'DELETE' }).catch(() => undefined);
        if (mountedRef.current) setConversationId(null);
      }
      if (reason instanceof DOMException && reason.name === 'AbortError') return;
      if (mountedRef.current) {
        setError(reason instanceof Error ? reason.message : '问答失败');
      }
    } finally {
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
      }
      if (mountedRef.current) {
        setLiveAnalysis(null);
        setLoading(false);
      }
    }
  };

  const cancelAsk = () => {
    requestAbortRef.current?.abort();
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
    <main className="ask-shell mx-auto flex w-full max-w-[1600px] gap-5 overflow-hidden px-3 py-3 sm:px-4 lg:px-6">
      <aside className="hidden w-64 shrink-0 lg:block">
        <div className="sticky top-5 space-y-3">
          <section className="rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
            <div className="flex items-center justify-between px-1">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
                问答记录
              </p>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => setHistoryOpen(true)} className="text-xs text-slate-500 hover:text-slate-950">管理</button>
                <button type="button" onClick={startNewConversation} className="text-xs font-medium text-slate-700 hover:text-slate-950">＋ 新建</button>
              </div>
            </div>
            <div className="mt-2 max-h-56 space-y-1 overflow-y-auto">
              {conversations.length === 0 ? (
                <p className="px-2 py-3 text-xs text-slate-400">完成一次提问后会保存到云端</p>
              ) : conversations.slice(0, 12).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => void loadConversation(item.id)}
                  className={`w-full rounded-xl px-2.5 py-2 text-left ${conversationId === item.id ? 'bg-slate-100' : 'hover:bg-slate-50'}`}
                >
                  <span className="block truncate text-xs font-medium text-slate-700">{item.title}</span>
                  <span className="mt-0.5 block text-[10px] text-slate-400">{item.turn_count} 条问答</span>
                </button>
              ))}
            </div>
          </section>
          <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
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
            记录会同步到云端，但每次提问仍独立检索，不会自动读取此前问答。
          </p>
          <Link
            href="/search"
            className="mt-4 block text-xs font-medium text-slate-700 hover:text-slate-950"
          >
            切换到精确搜索 →
          </Link>
          </div>
        </div>
      </aside>

      <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden border border-slate-200 bg-white shadow-sm sm:rounded-3xl">
        <header className="relative flex h-14 shrink-0 items-center gap-2 border-b border-slate-100 px-3 sm:h-auto sm:flex-wrap sm:justify-between sm:gap-3 sm:px-7 sm:py-4">
          <div className="hidden min-w-0 sm:block">
            <h1 className="text-xl font-semibold tracking-tight text-slate-950">
              问知识库
            </h1>
            <p className="mt-1 text-xs text-slate-500">
              {selectedScope?.name ?? '全部知识'} · 检索证据后回答
            </p>
          </div>
          <div className="flex min-w-0 flex-1 items-center gap-2 sm:hidden">
            <Link
              href="/search"
              aria-label="返回搜索"
              className="grid h-9 w-9 shrink-0 place-items-center rounded-xl text-lg text-slate-600 hover:bg-slate-100"
            >
              ←
            </Link>
            <button
              type="button"
              onClick={() => setFilterOpen(true)}
              className="flex min-w-0 flex-1 items-center gap-1.5 rounded-xl px-2 py-2 text-left hover:bg-slate-100"
            >
              <span className="truncate text-sm font-semibold text-slate-900">
                {selectedScope?.name ?? '全部知识'}
              </span>
              {refinementCount > 0 && (
                <span className="shrink-0 rounded-full bg-slate-900 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                  {refinementCount}
                </span>
              )}
              <span className="shrink-0 text-xs text-slate-400">⌄</span>
            </button>
          </div>
          <button
            type="button"
            onClick={() => setMode((current) => current === 'quick' ? 'deep' : 'quick')}
            className={`shrink-0 rounded-xl px-3 py-2 text-xs font-semibold sm:hidden ${
              mode === 'deep'
                ? 'bg-indigo-50 text-indigo-700'
                : 'bg-slate-100 text-slate-700'
            }`}
            title="切换问答模式"
          >
            {mode === 'deep' ? '深度' : '快速'}
          </button>
          <details className="group relative shrink-0 sm:hidden">
            <summary className="grid h-9 w-9 cursor-pointer list-none place-items-center rounded-xl text-xl leading-none text-slate-600 hover:bg-slate-100" aria-label="更多操作">
              ⋯
            </summary>
            <div className="absolute right-0 z-30 mt-2 w-40 rounded-2xl border border-slate-200 bg-white p-2 text-sm shadow-xl shadow-slate-950/10">
              <button
                type="button"
                onClick={(event) => {
                  setFilterOpen(true);
                  event.currentTarget.closest('details')?.removeAttribute('open');
                }}
                className="w-full rounded-xl px-3 py-2 text-left text-slate-700 hover:bg-slate-100"
              >
                知识范围与筛选
              </button>
              <button
                type="button"
                onClick={(event) => {
                  setHistoryOpen(true);
                  event.currentTarget.closest('details')?.removeAttribute('open');
                }}
                className="w-full rounded-xl px-3 py-2 text-left text-slate-700 hover:bg-slate-100"
              >
                问答记录
              </button>
              <button
                type="button"
                onClick={(event) => {
                  startNewConversation();
                  event.currentTarget.closest('details')?.removeAttribute('open');
                }}
                className="w-full rounded-xl px-3 py-2 text-left text-slate-700 hover:bg-slate-100"
              >
                新建问答
              </button>
              <Link href="/search" className="block rounded-xl px-3 py-2 text-slate-700 hover:bg-slate-100">
                精确搜索
              </Link>
            </div>
          </details>
          <div className="hidden items-center gap-2 sm:flex">
            <div className="flex shrink-0 rounded-xl bg-slate-100 p-1" aria-label="问答模式">
              {(['quick', 'deep'] as const).map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setMode(item)}
                  className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                    mode === item
                      ? 'bg-white text-slate-900 shadow-sm'
                      : 'text-slate-500 hover:text-slate-800'
                  }`}
                  title={
                    item === 'quick'
                      ? '一次检索后快速回答'
                      : '规划并调用多次检索或表格计算后回答'
                  }
                >
                  {item === 'quick' ? '快速问答' : '深度分析'}
                </button>
              ))}
            </div>
            <select
              value={scopeSlug}
              onChange={(event) => changeScope(event.target.value)}
              className="min-w-28 max-w-40 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 lg:hidden"
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
              className={`shrink-0 rounded-xl border px-3 py-2 text-xs font-medium ${
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
                onClick={startNewConversation}
                className="shrink-0 rounded-xl border border-slate-200 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50"
              >
                新建问答
              </button>
            )}
          </div>
        </header>

        {historyOpen && (
          <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="问答记录">
            <button type="button" aria-label="关闭问答记录" onClick={() => setHistoryOpen(false)} className="absolute inset-0 bg-slate-950/35" />
            <section className="absolute inset-x-0 bottom-0 flex max-h-[82dvh] flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl sm:inset-x-auto sm:bottom-auto sm:left-1/2 sm:top-1/2 sm:max-h-[72vh] sm:w-[34rem] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-3xl">
              <header className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
                <div>
                  <h2 className="font-semibold text-slate-950">问答记录</h2>
                  <p className="mt-0.5 text-xs text-slate-500">云端保存，仅用于回看，不作为模型记忆</p>
                </div>
                <button type="button" onClick={startNewConversation} className="rounded-xl bg-slate-950 px-3 py-2 text-xs font-medium text-white">新建问答</button>
              </header>
              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {historyLoading ? (
                  <p className="p-4 text-sm text-slate-500">正在读取…</p>
                ) : conversations.length === 0 ? (
                  <p className="p-4 text-sm text-slate-500">还没有云端问答记录。</p>
                ) : conversations.map((item) => (
                  <div key={item.id} className={`mb-1 flex items-center gap-2 rounded-2xl p-2 ${conversationId === item.id ? 'bg-slate-100' : ''}`}>
                    <button type="button" onClick={() => void loadConversation(item.id)} className="min-w-0 flex-1 px-2 py-1 text-left">
                      <span className="block truncate text-sm font-medium text-slate-800">{item.title}</span>
                      <span className="mt-1 block text-xs text-slate-400">{item.turn_count} 条问答</span>
                    </button>
                    <button
                      type="button"
                      aria-label={`删除 ${item.title}`}
                      onClick={() => {
                        if (window.confirm('删除这条问答记录？此操作不可恢复。')) {
                          void deleteConversation(item.id).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '删除失败'));
                        }
                      }}
                      className="rounded-xl px-3 py-2 text-xs text-red-600 hover:bg-red-50"
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
              <footer className="border-t border-slate-200 px-5 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
                <button type="button" onClick={() => setHistoryOpen(false)} className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm text-slate-700">返回当前问答</button>
              </footer>
            </section>
          </div>
        )}

        {filterOpen && (
          <>
            <div className="hidden sm:block">
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
            </div>
            <div
              className="fixed inset-0 z-50 sm:hidden"
              role="dialog"
              aria-modal="true"
              aria-label="知识范围与筛选"
            >
              <button
                type="button"
                aria-label="关闭知识范围"
                onClick={() => setFilterOpen(false)}
                className="absolute inset-0 bg-slate-950/35 backdrop-blur-[1px]"
              />
              <section className="absolute inset-x-0 bottom-0 flex max-h-[88dvh] flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl">
                <header className="flex shrink-0 items-center justify-between border-b border-slate-200 px-5 py-4">
                  <div>
                    <h2 className="text-base font-semibold text-slate-950">知识范围</h2>
                    <p className="mt-0.5 text-xs text-slate-500">选择本轮对话可以使用的知识</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setFilterOpen(false)}
                    className="rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-700"
                  >
                    关闭
                  </button>
                </header>
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                  <div className="border-b border-slate-100 px-5 py-4">
                    <p className="mb-2 text-xs font-semibold text-slate-600">基础范围</p>
                    <div className="flex flex-wrap gap-2">
                      {scopes.map((scope) => (
                        <button
                          key={`mobile-${scope.slug}`}
                          type="button"
                          onClick={() => changeScope(scope.slug)}
                          className={`rounded-full border px-3 py-1.5 text-xs font-medium ${
                            scopeSlug === scope.slug
                              ? 'border-slate-900 bg-slate-900 text-white'
                              : 'border-slate-200 bg-white text-slate-600'
                          }`}
                        >
                          {scope.name}
                        </button>
                      ))}
                    </div>
                  </div>
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
                </div>
                <footer className="shrink-0 border-t border-slate-200 bg-white px-5 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
                  <button
                    type="button"
                    onClick={() => setFilterOpen(false)}
                    className="w-full rounded-xl bg-slate-950 px-4 py-3 text-sm font-semibold text-white"
                  >
                    完成{refinementCount > 0 ? ` · 已选择 ${refinementCount} 项` : ''}
                  </button>
                </footer>
              </section>
            </div>
          </>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-slate-50/60 px-3 py-4 sm:px-7 sm:py-6">
          {turns.length === 0 && !loading && (
            <WelcomeState
              disabled={providerReady === false}
              onExample={setQuestion}
            />
          )}
          <div className="mx-auto w-full max-w-5xl space-y-6 sm:space-y-8">
            {turns.map((turn) => (
              <ConversationTurn
                key={turn.id}
                turn={turn}
                onOpenCitation={setSelectedCitation}
              />
            ))}
            {liveAnalysis ? (
              <LiveAnalysisCard analysis={liveAnalysis} />
            ) : loading ? (
              <div className="flex gap-3">
                <AssistantMark />
                <div className="rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 shadow-sm">
                  正在检索、核对出处并组织回答…
                </div>
              </div>
            ) : null}
          </div>
          <div ref={endRef} />
        </div>

        <footer className="shrink-0 border-t border-slate-100 bg-white px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-7 sm:py-4">
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
            {loading ? (
              <button
                type="button"
                onClick={cancelAsk}
                className="rounded-xl bg-red-50 px-4 py-3 text-sm font-medium text-red-700 hover:bg-red-100"
              >
                停止
              </button>
            ) : (
              <button
                type="submit"
                disabled={!question.trim() || providerReady === false}
                className="rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                发送
              </button>
            )}
          </form>
          <p className="mt-2 text-center text-[10px] leading-4 text-slate-400 sm:text-[11px]">
            {mode === 'deep'
              ? '每次提问独立检索，不读取此前问答 · 深度分析只在持续获得新证据时继续调用工具'
              : '每次提问独立检索，不读取此前问答 · 请核对引用原文'}
          </p>
        </footer>
        <EvidenceDrawer
          key={selectedCitation ? `${selectedCitation.document_version_id}:${selectedCitation.chunk_id}` : 'closed'}
          citation={selectedCitation}
          onClose={() => setSelectedCitation(null)}
        />
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
    '按类别统计表格中的金额合计，并列出前三名',
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

async function readAskStream(
  response: Response,
  onProgress: (event: Extract<AskStreamEvent, { type: 'progress' }>) => void,
): Promise<AskResponse> {
  if (!response.body) throw new Error('浏览器不支持流式问答');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result: AskResponse | null = null;
  let completed = false;

  const consumeLine = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as AskStreamEvent;
    if (event.type === 'progress') onProgress(event);
    if (event.type === 'result') result = event.data;
    if (event.type === 'error') {
      throw new Error(event.error.message ?? '问答请求失败');
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) consumeLine(line);
      if (done) break;
    }
    if (buffer.trim()) consumeLine(buffer);
    if (!result) throw new Error('深度分析连接中断，请重试');
    completed = true;
    return result;
  } finally {
    if (!completed) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function progressMessage(step: AnalysisStep): string {
  if (step.status === 'degraded') return `${step.label}未完成，正在调整方案`;
  if (step.result_count === undefined) return `${step.label}已完成`;
  return `${step.label}，得到 ${step.result_count} 条结果`;
}

function LiveAnalysisCard({ analysis }: { analysis: LiveAnalysis }) {
  return (
    <article className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-slate-900 px-4 py-3 text-sm leading-6 text-white">
          {analysis.question}
        </div>
      </div>
      <div className="flex items-start gap-3">
        <AssistantMark />
        <div className="min-w-0 max-w-5xl flex-1 rounded-2xl rounded-tl-md border border-indigo-100 bg-white px-4 py-4 shadow-sm">
          <div className="flex items-center gap-2 text-sm font-medium text-indigo-800">
            <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-500" />
            {analysis.message}
          </div>
          <p className="mt-1 text-[11px] text-slate-400">
            范围：{analysis.contextLabel} · {analysis.toolCalls}/
            {analysis.maxToolCalls} 次工具调用
            {analysis.iterations ? ` · ${analysis.iterations} 轮决策` : ''}
          </p>
          {analysis.steps.length > 0 && (
            <ol className="mt-3 space-y-2 border-t border-slate-100 pt-3">
              {analysis.steps.map((step, index) => (
                <li
                  key={`${step.tool}-${index}`}
                  className="flex items-start gap-2 text-xs"
                >
                  <span className="mt-0.5 text-emerald-600">✓</span>
                  <div className="min-w-0">
                    <span className="font-medium text-slate-700">
                      {step.label}
                    </span>
                    <span className="ml-2 text-slate-400">
                      {step.status === 'degraded'
                        ? '正在调整'
                        : step.result_count === undefined
                          ? '已完成'
                          : `${step.result_count} 条结果`}
                    </span>
                  </div>
                </li>
              ))}
            </ol>
          )}
          {analysis.evidenceAudits.length > 0 && (
            <div className="mt-3 space-y-2 border-t border-slate-100 pt-3">
              {analysis.evidenceAudits.map((audit, index) => (
                <div key={`${audit.status}-${index}`} className="rounded-xl bg-amber-50 px-3 py-2 text-xs">
                  <span className="font-medium text-amber-800">证据审计：</span>
                  <span className="text-slate-600">{audit.summary}</span>
                  {audit.missing_evidence.length > 0 && (
                    <p className="mt-1 text-slate-500">
                      待补充：{audit.missing_evidence.join('、')}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
          <p className="mt-3 text-[11px] text-slate-400">
            展示的是可审计工具过程，不包含模型内部思维链。
          </p>
        </div>
      </div>
    </article>
  );
}

function ConversationTurn({
  turn,
  onOpenCitation,
}: {
  turn: Turn;
  onOpenCitation: (citation: Citation) => void;
}) {
  return (
    <article className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-slate-900 px-4 py-3 text-sm leading-6 text-white">
          {turn.question}
        </div>
      </div>
      <div className="flex items-start gap-3">
        <AssistantMark />
        <div className="min-w-0 max-w-5xl flex-1">
          <div className="prose prose-slate max-w-none rounded-2xl rounded-tl-md border border-slate-200 bg-white px-5 py-4 text-sm leading-7 shadow-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>
              {turn.response.answer}
            </ReactMarkdown>
          </div>
          <ResultMeta response={turn.response} contextLabel={turn.contextLabel} />
          {turn.response.retrieval?.analysis && (
            <AnalysisTrace analysis={turn.response.retrieval.analysis} />
          )}
          {turn.response.citations.length > 0 && (
            <CitationList
              citations={turn.response.citations}
              onOpen={onOpenCitation}
            />
          )}
        </div>
      </div>
    </article>
  );
}

function AnalysisTrace({
  analysis,
}: {
  analysis: NonNullable<NonNullable<AskResponse['retrieval']>['analysis']>;
}) {
  return (
    <details className="mt-3 rounded-2xl border border-indigo-100 bg-indigo-50/50">
      <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-indigo-800">
        深度分析过程 · {analysis.tool_calls} 次工具调用
        {analysis.iterations ? ` · ${analysis.iterations} 轮决策` : ''}
      </summary>
      <div className="border-t border-indigo-100 px-4 py-3">
        {analysis.plan_summary && (
          <p className="mb-2 text-xs text-slate-600">{analysis.plan_summary}</p>
        )}
        <ol className="space-y-2">
          {analysis.steps.map((step, index) => (
            <li key={`${step.tool}-${index}`} className="flex gap-2 text-xs">
              <span className="text-slate-400">{index + 1}.</span>
              <div className="min-w-0">
                <span className="font-medium text-slate-700">{step.label}</span>
                <span className="ml-2 text-slate-400">
                  {step.status === 'skipped'
                    ? '无需执行'
                    : step.status === 'degraded'
                      ? '已降级'
                      : step.result_count === undefined
                        ? '已完成'
                        : `得到 ${step.result_count} 条结果`}
                </span>
              </div>
            </li>
          ))}
        </ol>
        {(analysis.evidence_audits?.length ?? 0) > 0 && (
          <div className="mt-3 space-y-2 border-t border-indigo-100 pt-3">
            {analysis.evidence_audits?.map((audit, index) => (
              <div key={`${audit.status}-${index}`} className="rounded-xl bg-white/70 px-3 py-2 text-xs">
                <p className="font-medium text-amber-800">证据完整性检查</p>
                <p className="mt-1 text-slate-600">{audit.summary}</p>
                {audit.missing_evidence.length > 0 && (
                  <p className="mt-1 text-slate-500">
                    待补充：{audit.missing_evidence.join('、')}
                  </p>
                )}
                {audit.next_query && (
                  <p className="mt-1 text-slate-400">继续检索：{audit.next_query}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </details>
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
        {response.retrieval?.mode === 'deep_analysis'
          ? `深度分析 · ${response.retrieval.analysis?.tool_calls ?? 0} 次工具调用`
          : response.retrieval?.mode === 'structured_table'
          ? `表格精确计算 · ${response.retrieval.structured_table?.matched_rows ?? 0} 行`
          : response.retrieval?.vector_used
            ? '混合检索'
            : '关键词检索'}
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

function CitationList({
  citations,
  onOpen,
}: {
  citations: Citation[];
  onOpen: (citation: Citation) => void;
}) {
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
            onOpen={onOpen}
          />
        ))}
      </ol>
    </details>
  );
}

function CitationItem({
  citation,
  index,
  onOpen,
}: {
  citation: Citation;
  index: number;
  onOpen: (citation: Citation) => void;
}) {
  return (
    <li className="border-b border-slate-100 p-4 last:border-b-0">
      <button
        type="button"
        onClick={() => onOpen(citation)}
        className="w-full text-left"
      >
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
                  查看证据 →
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
                {citation.table_location?.sheet_name
                  ? ` · ${citation.table_location.sheet_name}`
                  : ''}
                {citation.table_location?.row_start
                  ? ` · 第 ${citation.table_location.row_start}${
                      citation.table_location.row_end &&
                      citation.table_location.row_end !== citation.table_location.row_start
                        ? `–${citation.table_location.row_end}`
                        : ''
                    } 行`
                  : ''}
              </p>
            </div>
          </div>
      </button>
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
