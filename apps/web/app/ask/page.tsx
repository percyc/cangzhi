'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import { askPreferencesKey, validateAskPreferences } from '@/lib/ask-search-state';
import { useUnsavedChanges } from '@/lib/navigation-guard';
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
      dataset_id?: number | null;
      document_id?: number;
      dataset_name?: string;
      sheet_name?: string;
      matched_rows: number;
      metric: string;
      metric_column?: string | null;
      group_by?: string[];
      columns?: string[];
      aggregate?: Record<string, unknown>;
      contributions?: Array<Record<string, unknown>>;
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

type DatasetSummary = {
  dataset_count: number;
  document_count: number;
  ready_count: number;
  examples: Array<{ id: number; name: string; sheet_name: string }>;
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
  const documentSelection = searchParams.get('document_ids') ?? '';
  const parsedDocumentIds = documentSelection.split(',').filter(Boolean).map(Number);
  const [documentIds, setDocumentIds] = useState<number[]>(parsedDocumentIds.every((id) => Number.isSafeInteger(id) && id > 0) && parsedDocumentIds.length <= 50 ? [...new Set(parsedDocumentIds)] : []);
  const [invalidDocumentSelection, setInvalidDocumentSelection] = useState(Boolean(documentSelection) && (parsedDocumentIds.length === 0 || parsedDocumentIds.length > 50 || parsedDocumentIds.some((id) => !Number.isSafeInteger(id) || id <= 0)));
  const [question, setQuestion] = useState(searchParams.get('q') ?? '');
  const [mode, setMode] = useState<'quick' | 'deep'>('quick');
  const [scopeSlug, setScopeSlug] = useState('all');
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [facets, setFacets] = useState<FacetCatalog | null>(null);
  const [datasetSummary, setDatasetSummary] = useState<DatasetSummary | null>(null);
  const [categoryIds, setCategoryIds] = useState<number[]>([]);
  const [tagIds, setTagIds] = useState<number[]>([]);
  const [sourceTypes, setSourceTypes] = useState<string[]>([]);
  const [connectorIds, setConnectorIds] = useState<number[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [preferencesNotice, setPreferencesNotice] = useState<string | null>(null);
  const [draftStorageAvailable, setDraftStorageAvailable] = useState(true);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [mobileArea, setMobileArea] = useState<'chat' | 'library' | 'evidence'>('chat');
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [liveAnalysis, setLiveAnalysis] = useState<LiveAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [providerReady, setProviderReady] = useState<boolean | null>(null);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const requestAbortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const viewRevisionRef = useRef(0);
  const questionRevisionRef = useRef(0);
  const questionRef = useRef(question);
  const conversationRef = useRef(conversationId);
  const preferencesKeyRef = useRef<string | null>(null);
  const conversationListRevisionRef = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const citationTriggerRef = useRef<HTMLElement | null>(null);
  const followOutputRef = useRef(true);
  questionRef.current = question;
  conversationRef.current = conversationId;
  useUnsavedChanges(!draftStorageAvailable && Boolean(question.trim()), '浏览器未能保存问答草稿，离开会丢失输入。确定离开吗？');
  const askHref = () => documentIds.length ? `/ask?document_ids=${documentIds.join(',')}` : '/ask';

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      viewRevisionRef.current += 1;
      requestAbortRef.current?.abort();
      historyAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (sessionReady || !facets || scopes.length === 0) return;
    const timer = window.setTimeout(() => {
      const key = askPreferencesKey(document.cookie, documentIds);
      preferencesKeyRef.current = key;
      try {
        const saved = window.sessionStorage.getItem(key);
        const { preferences, filtersRemoved } = validateAskPreferences(saved ? JSON.parse(saved) : null, { ...facets, scopes });
        setScopeSlug(documentSelection ? 'all' : preferences.scopeSlug);
        setCategoryIds(documentSelection ? [] : preferences.categoryIds);
        setTagIds(documentSelection ? [] : preferences.tagIds);
        setSourceTypes(documentSelection ? [] : preferences.sourceTypes);
        setConnectorIds(documentSelection ? [] : preferences.connectorIds);
        setMode(preferences.mode);
        if (!searchParams.get('q') && questionRevisionRef.current === 0) {
          setQuestion(preferences.question);
        }
        if (filtersRemoved) setPreferencesNotice('已移除当前空间中失效的知识范围或筛选项，请确认范围后再提问。');
      } catch {
        // Storage can be unavailable in private or restricted browser contexts.
        setDraftStorageAvailable(false);
      } finally {
        setSessionReady(true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [facets, scopes, searchParams, sessionReady, documentSelection, documentIds]);

  useEffect(() => {
    if (!sessionReady || !preferencesKeyRef.current) return;
    try { window.sessionStorage.setItem(
      askPreferencesKey(document.cookie, documentIds),
      JSON.stringify({
        scopeSlug,
        categoryIds,
        tagIds,
        sourceTypes,
        connectorIds,
        question,
        mode,
      }),
    ); } catch {
      // Surface a failed external storage write so navigation can protect the draft.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setDraftStorageAvailable(false);
    }
  }, [
    scopeSlug,
    categoryIds,
    tagIds,
    sourceTypes,
    connectorIds,
    question,
    mode,
    sessionReady,
    documentIds,
  ]);

  useEffect(() => {
    if (!sessionReady) return;
    let cancelled = false;
    const revision = viewRevisionRef.current;
    const listRevision = ++conversationListRevisionRef.current;
    const load = async () => {
      try {
        const response = await fetch('/api/ask/conversations', { cache: 'no-store' });
        if (!response.ok) throw new Error('问答记录加载失败');
        const payload = (await response.json()) as { items: ConversationSummary[] };
        if (cancelled || listRevision !== conversationListRevisionRef.current) return;
        setConversations(payload.items);
        if (!documentSelection && !searchParams.get('q') && !questionRef.current && questionRevisionRef.current === 0 && revision === viewRevisionRef.current && payload.items[0]) {
          await loadConversation(payload.items[0].id);
        }
      } catch (reason) {
        if (!cancelled && revision === viewRevisionRef.current) setError(reason instanceof Error ? reason.message : '问答记录加载失败');
      }
    };
    void load();
    return () => { cancelled = true; };
  // Restore local workspace preferences before deciding whether to open history.
  // Do not reopen history when a later URL or input change occurs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionReady]);

  useEffect(() => {
    let cancelled = false;
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
      fetch('/api/datasets/summary', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('数据集状态加载失败');
          return (await response.json()) as DatasetSummary;
        },
      ).catch(() => null),
    ])
      .then(([scopeResult, status, facetResult, datasetResult]) => {
        if (cancelled) return;
        setScopes(scopeResult.items);
        setProviderReady(status.provider_configured);
        setFacets(facetResult);
        setDatasetSummary(datasetResult);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : '初始化失败');
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (followOutputRef.current && mobileArea === 'chat') endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  // Changing workbench areas must not move the preserved reading position.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns, loading, liveAnalysis]);

  const refreshConversations = async (revision = viewRevisionRef.current) => {
    const listRevision = ++conversationListRevisionRef.current;
    const response = await fetch('/api/ask/conversations', { cache: 'no-store' });
    if (!response.ok) throw new Error('问答记录加载失败');
    const payload = (await response.json()) as { items: ConversationSummary[] };
    if (mountedRef.current && revision === viewRevisionRef.current && listRevision === conversationListRevisionRef.current) setConversations(payload.items);
  };

  const invalidateViewRequests = () => {
    viewRevisionRef.current += 1;
    requestAbortRef.current?.abort();
    requestAbortRef.current = null;
    historyAbortRef.current?.abort();
    historyAbortRef.current = null;
    setLoading(false);
    setLiveAnalysis(null);
    setHistoryLoading(false);
    setSelectedCitation(null);
    setEvidenceOpen(false);
    return viewRevisionRef.current;
  };

  const editQuestion = (value: string) => {
    questionRevisionRef.current += 1;
    questionRef.current = value;
    setQuestion(value);
  };

  async function loadConversation(id: number) {
    const revision = invalidateViewRequests();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    setConversationId(id);
    conversationRef.current = id;
    setTurns([]);
    setError(null);
    setHistoryLoading(true);
    setMobileArea('chat');
    followOutputRef.current = true;
    const isCurrent = () => mountedRef.current && viewRevisionRef.current === revision && !controller.signal.aborted;
    try {
      const response = await fetch(`/api/ask/conversations/${id}`, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('问答记录读取失败');
      const payload = (await response.json()) as ConversationDetail;
      if (!isCurrent()) return;
      setConversationId(payload.id);
      setTurns(payload.turns.map((turn) => ({
        id: turn.id,
        question: turn.question,
        response: turn.response,
        contextLabel: turn.context_label ?? '全部知识',
        mode: turn.mode,
      })));
      setError(null);
      window.history.replaceState(null, '', askHref());
    } catch (reason) {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : '问答记录读取失败');
    } finally {
      if (isCurrent()) {
        historyAbortRef.current = null;
        setHistoryLoading(false);
      }
    }
  }

  const startNewConversation = () => {
    if (questionRef.current.trim() && !window.confirm('新建会清空当前输入，已有问答记录不会删除。是否继续？')) return;
    invalidateViewRequests();
    conversationRef.current = null;
    setConversationId(null);
    setTurns([]);
    editQuestion('');
    setError(null);
    setMobileArea('chat');
    followOutputRef.current = true;
    window.history.replaceState(null, '', askHref());
  };

  const deleteConversation = async (id: number) => {
    const response = await fetch(`/api/ask/conversations/${id}`, { method: 'DELETE' });
    if (!response.ok) throw new Error('删除问答记录失败');
    if (conversationRef.current === id) {
      invalidateViewRequests();
      conversationRef.current = null;
      setConversationId(null);
      setTurns([]);
      setMobileArea('chat');
      setError(null);
      window.history.replaceState(null, '', askHref());
      // Deleting saved history does not authorize discarding the current input.
    }
    await refreshConversations();
  };

  const runAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || invalidDocumentSelection || !sessionReady || historyLoading || loading || requestAbortRef.current) return;
    const revision = invalidateViewRequests();
    const questionRevision = questionRevisionRef.current;
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const isCurrent = () => mountedRef.current && viewRevisionRef.current === revision && requestAbortRef.current === controller && !controller.signal.aborted;
    setLoading(true);
    followOutputRef.current = true;
    setError(null);
    const contextLabel = (documentIds.length ? `指定资料 ${documentIds.map((id) => `#${id}`).join('、')} · ` : '') + buildContextLabel(
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
        if (!isCurrent()) return;
        targetConversationId = created.id;
        conversationRef.current = created.id;
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
        document_ids: documentIds,
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
              if (!isCurrent()) return;
              setLiveAnalysis((current) => {
                if (!current || !isCurrent()) return current;
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
      if (!isCurrent()) return;
      setTurns((current) => viewRevisionRef.current !== revision ? current : [
        ...current,
        {
          id: body.history?.turn_id ?? Date.now(),
          question: trimmed,
          response: body as AskResponse,
          contextLabel,
          mode,
        },
      ]);
      if (questionRevision === questionRevisionRef.current) setQuestion('');
      window.history.replaceState(null, '', askHref());
      void refreshConversations(revision).catch(() => undefined);
    } catch (reason) {
      // A lost response does not prove the server failed to save the answer.
      // Never delete a conversation automatically on cancellation/network failure.
      if (isCurrent()) {
        setError(reason instanceof Error ? reason.message : '问答失败');
      }
    } finally {
      if (isCurrent()) {
        requestAbortRef.current = null;
        setLiveAnalysis(null);
        setLoading(false);
      }
    }
  };

  const cancelAsk = () => {
    invalidateViewRequests();
    setError(null);
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
  const openCitation = (citation: Citation) => {
    citationTriggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelectedCitation(citation);
    setEvidenceOpen(true);
    setMobileArea('evidence');
  };
  const closeEvidence = () => {
    setEvidenceOpen(false);
    setMobileArea('chat');
    window.requestAnimationFrame(() => {
      if (citationTriggerRef.current?.isConnected) citationTriggerRef.current.focus({ preventScroll: true });
      else inputRef.current?.focus({ preventScroll: true });
    });
  };

  return (
    <main className="ask-shell mx-auto flex w-full max-w-[1800px] flex-col gap-3 overflow-hidden">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <button type="button" aria-controls="ask-library" aria-expanded={historyOpen} onClick={() => setHistoryOpen((value) => !value)} className="hidden rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600 lg:block">{historyOpen ? '收起侧栏' : '范围与记录'}</button>
          <h1 className="text-lg font-semibold text-slate-900">问知识库</h1>
          <Link href="/search" className="rounded-lg px-2 py-2 text-xs text-slate-500 hover:bg-white">搜资料 ↗</Link>
        </div>
        <div className="flex items-center gap-2">
          <div className="hidden rounded-xl bg-slate-100 p-1 sm:flex" aria-label="问答模式">
            {(['quick', 'deep'] as const).map((item) => (
              <button key={item} type="button" disabled={!sessionReady} onClick={() => setMode(item)} aria-pressed={mode === item} className={`rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${mode === item ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>{item === 'quick' ? '快速问答' : '深度分析'}</button>
            ))}
          </div>
          <button type="button" disabled={!sessionReady} onClick={() => setMode((current) => current === 'quick' ? 'deep' : 'quick')} title="切换问答模式" className="rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-700 disabled:opacity-40 sm:hidden">{mode === 'deep' ? '深度' : '快速'}</button>
          <button type="button" aria-controls="ask-evidence" aria-expanded={evidenceOpen} onClick={() => { setEvidenceOpen((value) => !value); setMobileArea('chat'); }} className="hidden rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600 lg:block">{evidenceOpen ? '收起证据栏' : '证据工作区'}</button>
          <button type="button" onClick={startNewConversation} className="rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white">＋ 新建</button>
        </div>
      </header>
      <nav aria-label="问答工作区" className="grid shrink-0 grid-cols-3 gap-1 rounded-xl bg-slate-100 p-1 lg:hidden">
        {([{ id: 'chat', label: '问答' }, { id: 'library', label: '范围与记录' }, { id: 'evidence', label: '引用证据' }] as const).map((area) => (
          <button key={area.id} type="button" aria-pressed={mobileArea === area.id} aria-controls={`ask-${area.id === 'chat' ? 'conversation' : area.id}`} onClick={() => { setMobileArea(area.id); if (area.id === 'evidence') setEvidenceOpen(true); }} className={`rounded-lg px-2 py-2 text-xs font-medium ${mobileArea === area.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>{area.label}{area.id === 'evidence' && selectedCitation ? ' · 1' : ''}</button>
        ))}
      </nav>
      <div className="flex min-h-0 flex-1 gap-3">
        <aside id="ask-library" aria-label="知识范围与问答记录" className={`${mobileArea === 'library' ? 'flex' : 'hidden'} ${historyOpen ? 'lg:flex' : 'lg:hidden'} min-h-0 w-full shrink-0 flex-col overflow-y-auto rounded-2xl border border-slate-200 bg-white p-3 lg:w-60 xl:w-64`}>
          <section>
            <div className="flex items-center justify-between gap-2 px-1">
              <h2 className="text-xs font-semibold text-slate-500">问答记录</h2>
              <span className="text-[10px] text-slate-400">云端保存</span>
            </div>
            <p className="mt-2 px-1 text-[11px] leading-5 text-slate-400">记录用于回看；每次提问独立检索，不读取此前问答。</p>
            <div className="mt-2 max-h-64 space-y-1 overflow-y-auto">
              {conversations.length === 0 ? <p className="px-2 py-3 text-xs text-slate-400">完成一次提问后会保存到云端</p> : conversations.map((item) => (
                <div key={item.id} className={`group flex items-center gap-1 rounded-xl ${conversationId === item.id ? 'bg-slate-100' : 'hover:bg-slate-50'}`}>
                  <button type="button" onClick={() => void loadConversation(item.id)} aria-current={conversationId === item.id ? 'true' : undefined} className="min-w-0 flex-1 px-2.5 py-2 text-left"><span className="block truncate text-xs font-medium text-slate-700">{item.title}</span><span className="mt-0.5 block text-[10px] text-slate-400">{item.turn_count} 条问答</span></button>
                  <button type="button" aria-label={`删除记录：${item.title}`} onClick={() => { if (window.confirm('删除这份问答记录？资料与知识库不受影响。')) void deleteConversation(item.id).catch((reason) => setError(reason instanceof Error ? reason.message : '删除失败')); }} className="rounded-lg px-2 py-2 text-xs text-slate-400 hover:bg-red-50 hover:text-red-600 focus:text-red-600">×</button>
                </div>
              ))}
            </div>
          </section>
          <section className="mt-4 border-t border-slate-100 pt-4">
            <h2 className="px-1 text-xs font-semibold text-slate-500">本次提问的知识范围</h2>
            <select aria-label="知识范围" disabled={!sessionReady} value={scopeSlug} onChange={(event) => changeScope(event.target.value)} className="mt-3 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm lg:hidden">{scopes.map((scope) => <option key={scope.slug} value={scope.slug}>{scope.name}</option>)}</select>
            <div className="mt-2 hidden space-y-1 lg:block">{scopes.map((scope) => <button key={scope.slug} type="button" disabled={!sessionReady} onClick={() => changeScope(scope.slug)} aria-pressed={scopeSlug === scope.slug} className={`w-full rounded-xl px-3 py-2 text-left text-sm disabled:opacity-40 ${scopeSlug === scope.slug ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-50'}`}>{scope.name}</button>)}</div>
            <button type="button" disabled={!sessionReady} aria-expanded={filterOpen} onClick={() => setFilterOpen((value) => !value)} className="mt-3 w-full rounded-xl border border-slate-200 px-3 py-2 text-left text-xs text-slate-600 disabled:opacity-40">细化范围{refinementCount ? ` · ${refinementCount}` : ''}</button>
            <button type="button" disabled={!sessionReady} onClick={() => setFilterOpen(true)} className="mt-2 text-xs text-slate-500 disabled:opacity-40 lg:hidden">知识范围与筛选</button>
            {filterOpen && sessionReady && <FacetPanel facets={facets} selectedScope={selectedScope} categoryIds={categoryIds} tagIds={tagIds} sourceTypes={sourceTypes} connectorIds={connectorIds} setCategoryIds={setCategoryIds} setTagIds={setTagIds} setSourceTypes={setSourceTypes} setConnectorIds={setConnectorIds} onClear={clearRefinements} />}
          </section>
        </aside>
        <section id="ask-conversation" aria-label="知识问答" className={`${mobileArea === 'chat' ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white lg:flex`}>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
            <p className="text-xs text-slate-500">{selectedScope?.name ?? '全部知识'}{refinementCount ? ` · ${refinementCount} 项筛选` : ''} · 检索证据后回答</p>
            {(documentIds.length > 0 || invalidDocumentSelection) && <div className="flex flex-wrap items-center gap-2 rounded-lg bg-blue-50 px-2 py-1 text-xs text-blue-800"><span>{invalidDocumentSelection ? '资料范围无效，请清除后重新选择' : `仅问指定资料 ${documentIds.map((id) => `#${id}`).join('、')}`}</span><button type="button" onClick={() => { setDocumentIds([]); setInvalidDocumentSelection(false); window.history.replaceState(null, '', '/ask'); }} className="font-semibold underline">清除限定</button></div>}
          </div>

        <div onScroll={(event) => { const node = event.currentTarget; followOutputRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 96; }} className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-slate-50/60 px-3 py-4 sm:px-5 sm:py-6">
          {turns.length === 0 && !loading && (
            <WelcomeState
              disabled={providerReady === false}
              onExample={editQuestion}
            />
          )}
          <div className="mx-auto w-full max-w-5xl space-y-6 sm:space-y-8">
            {turns.map((turn) => (
              <ConversationTurn
                key={turn.id}
                turn={turn}
                onOpenCitation={openCitation}
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
          {!sessionReady && !error && (
            <p role="status" className="mb-3 text-xs text-slate-500">正在加载当前空间的知识范围与问答偏好，你可以先输入问题…</p>
          )}
          {preferencesNotice && (
            <p role="status" className="mb-3 rounded-xl bg-amber-50 px-4 py-3 text-xs text-amber-800">{preferencesNotice}</p>
          )}
          {historyLoading && <p role="status" className="mb-3 text-sm text-slate-500">正在读取所选问答记录…</p>}
          {error && (
            <p className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </p>
          )}
          {!draftStorageAvailable && <p role="alert" className="mb-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">浏览器无法保存草稿，请在离开前复制输入。发送问答仍可正常使用。</p>}
          {datasetSummary && datasetSummary.dataset_count > 0 && (
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 px-1 text-[11px] leading-5 text-slate-500">
              <span>
                当前知识库有 {datasetSummary.dataset_count} 个可查询数据区域；快速问答会自动使用
                DuckDB 精确筛选和统计。
              </span>
              {mode === 'quick' && (
                <button
                  type="button"
                  disabled={!sessionReady}
                  onClick={() => setMode('deep')}
                  className="font-medium text-indigo-700 hover:underline"
                >
                  跨表或多步计算改用深度分析
                </button>
              )}
            </div>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void runAsk();
            }}
            className="flex items-end gap-3 rounded-2xl border border-slate-200 bg-slate-50 p-2 focus-within:border-slate-400 focus-within:bg-white"
          >
            <textarea
              ref={inputRef}
              value={question}
              onChange={(event) => editQuestion(event.target.value)}
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
              aria-label="向当前知识范围提问"
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
                disabled={!question.trim() || invalidDocumentSelection || !sessionReady || historyLoading || providerReady === false}
                className="rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                发送
              </button>
            )}
          </form>
          <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-slate-400"><span title="按工作空间与指定资料范围保存在本浏览器当前标签页；关闭标签页后可能丢失，不是云端草稿。">{draftStorageAvailable ? '草稿保留在本标签页 · 按空间与资料范围隔离' : '草稿尚未保存'}</span><button type="button" disabled={!question} onClick={() => editQuestion('')} className="px-1 py-1 hover:text-slate-700 disabled:opacity-40">清空草稿</button></div>
          <p className="mt-2 text-center text-[10px] leading-4 text-slate-400 sm:text-[11px]">
            <span className="hidden sm:inline">Enter 发送 · Shift+Enter 换行 · </span>{mode === 'deep'
              ? '每次提问独立检索，不读取此前问答 · 深度分析只在持续获得新证据时继续调用工具'
              : '每次提问独立检索，不读取此前问答 · 请核对引用原文'}
          </p>
        </footer>
      </section>
      <aside id="ask-evidence" aria-label="证据工作区" className={`${evidenceOpen && mobileArea === 'evidence' ? 'flex' : 'hidden'} ${evidenceOpen ? 'lg:flex' : 'lg:hidden'} min-h-0 w-full min-w-0 shrink-0 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white lg:w-[40%] lg:max-w-[720px]`}>
        <EvidenceDrawer citation={selectedCitation} active={evidenceOpen && mobileArea === 'evidence'} onClose={closeEvidence} />
      </aside>
      </div>
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
      <details className="mt-4 w-full rounded-xl border border-slate-200 bg-slate-50 p-3 text-left text-xs leading-6 text-slate-600">
        <summary className="cursor-pointer font-medium text-slate-700">如何选择问答模式与范围？</summary>
        <p className="mt-2">顶栏工作空间决定可用资料；知识范围与筛选进一步缩小范围。快速问答适合单个问题，深度分析适合多步查证或数据计算，可能调用模型多次。</p>
        <p>完成后点击引用核对原文。每次提问独立检索，问答记录不会自动成为模型记忆。</p>
      </details>
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
      <div className="rounded-xl bg-slate-50 px-3 py-4 text-xs text-slate-500">
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
    <div className="mt-3 rounded-xl bg-slate-50/80 px-3 py-4">
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

      <div className="mt-4 grid gap-4">
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
          {turn.response.retrieval?.mode === 'structured_table' &&
            turn.response.retrieval.structured_table && (
              <DatasetResultPreview
                result={turn.response.retrieval.structured_table}
              />
            )}
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

function DatasetResultPreview({
  result,
}: {
  result: NonNullable<
    NonNullable<AskResponse['retrieval']>['structured_table']
  >;
}) {
  const rows = result.contributions ?? [];
  const columns = Array.from(
    new Set(rows.flatMap((row) => Object.keys(row))),
  );
  const aggregate = result.aggregate ?? {};
  const aggregateValue = aggregate.value;
  const metricLabels: Record<string, string> = {
    rows: '明细',
    count: '数量',
    count_distinct: '去重数量',
    sum: '合计',
    avg: '平均值',
    min: '最小值',
    max: '最大值',
  };
  return (
    <details open className="mt-3 overflow-hidden rounded-2xl border border-emerald-200 bg-emerald-50/40">
      <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-emerald-900">
        精确查询结果 · {result.dataset_name ?? '数据集'}
        {result.sheet_name ? ` / ${result.sheet_name}` : ''}
      </summary>
      <div className="border-t border-emerald-100 bg-white px-4 py-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
          <span className="rounded-full bg-emerald-50 px-2 py-1 text-emerald-800">
            {metricLabels[result.metric] ?? result.metric}
            {result.metric_column ? `：${result.metric_column}` : ''}
          </span>
          <span>命中 {result.matched_rows.toLocaleString('zh-CN')} 行</span>
          {aggregateValue !== undefined && aggregateValue !== null && (
            <span className="font-semibold text-slate-900">
              结果：{formatDatasetValue(aggregateValue)}
            </span>
          )}
          {result.document_id && (
            <Link href={`/documents/${result.document_id}`} className="text-blue-700 hover:underline">
              查看来源资料
            </Link>
          )}
        </div>
        {rows.length > 0 && columns.length > 0 && (
          <div className="mt-3 max-h-72 overflow-auto rounded-lg border border-slate-200">
            <table className="min-w-full text-left text-xs">
              <thead className="sticky top-0 bg-slate-50 text-slate-500">
                <tr>
                  {columns.map((column) => (
                    <th key={column} className="whitespace-nowrap px-3 py-2 font-medium">{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr key={index} className="border-t border-slate-100">
                    {columns.map((column) => (
                      <td key={column} className="max-w-72 whitespace-pre-wrap break-words px-3 py-2 text-slate-700">
                        {formatDatasetValue(row[column])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(result.warnings?.length ?? 0) > 0 && (
          <ul className="mt-3 space-y-1 text-xs text-amber-700">
            {result.warnings?.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        )}
        <p className="mt-3 text-[11px] leading-5 text-slate-400">
          结果由受控查询计划经程序校验后执行，不是模型根据表格内容估算。
        </p>
      </div>
    </details>
  );
}

function formatDatasetValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
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
