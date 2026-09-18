'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { withApiBasePath } from '@/lib/paths';
import EnhancementOverview from '@/components/EnhancementOverview';

type RunStatus =
  | 'queued'
  | 'running'
  | 'partial'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'stale';

type Hierarchy = {
  enabled: boolean;
  status: string;
  total_nodes: number;
  completed_nodes: number;
  root_key: string | null;
};

type Run = {
  id: number;
  document_id: number;
  document_version_id: number;
  status: RunStatus;
  call_budget: number;
  calls_used: number;
  total_windows: number;
  completed_windows: number;
  failed_windows: number;
  total_source_chars: number;
  completed_source_chars: number;
  last_error: string | null;
  created_at: string;
  lease_until: string | null;
  hierarchy: Hierarchy;
};

type EvidenceResult = {
  summary: { text: string; evidence_ids: number[] } | null;
  entities: Array<{
    id: string;
    name: string;
    kind: string;
    aliases: string[];
    evidence_ids: number[];
  }> | null;
  relations: Array<{
    subject: string;
    object: string;
    predicate: string;
    evidence_ids: number[];
  }> | null;
  events: Array<{ text: string; evidence_ids: number[] }> | null;
  evidence_status: 'model_extracted_unverified' | null;
};

type SourceSegment = {
  id: number;
  text: string;
  block_id: string;
  start: number;
  stop: number;
  page: number | null;
  heading_path: string[];
};

type WindowDetail = {
  ordinal: number;
  status: string;
  last_error: string | null;
  result: EvidenceResult | null;
  source_segments: SourceSegment[];
};

type RunDetail = {
  run: Run;
  windows: WindowDetail[];
  total_windows: number;
  next_offset: number | null;
};

const PAGE_SIZE = 5;
const POLL_MS = 5000;
const RUNNING_STATUS: RunStatus[] = ['queued', 'running'];
const RESUMEABLE: RunStatus[] = ['failed', 'cancelled', 'partial'];
const MAX_BUDGET = 32;

const RUN_LABELS: Record<RunStatus, string> = {
  queued: '排队中',
  running: '处理中',
  partial: '部分完成',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  stale: '已过期',
};

type EvidenceSource = {
  id: string;
  label: string;
  evidence_ids: number[];
};

function evidenceInline(segment: SourceSegment) {
  const path = segment.heading_path.length
    ? `${segment.heading_path.join(' › ')} · `
    : '';
  const page = segment.page != null ? `第 ${segment.page} 页 · ` : '';
  return `${path}${page}${segment.text}`;
}

function EvidenceBlock({
  evidenceIds,
  segments,
  title,
}: {
  evidenceIds: number[];
  segments: SourceSegment[];
  title: string;
}) {
  if (!segments.length) return null;
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-xs text-amber-700">
        {title} · 原文证据 {evidenceIds.length} 处
      </summary>
      <div className="mt-2 space-y-2">
        {segments.map((segment) => (
          <blockquote
            key={segment.id}
            className="whitespace-pre-wrap rounded border-l-4 border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-6 text-slate-700"
          >
            {evidenceInline(segment)}
          </blockquote>
        ))}
      </div>
    </details>
  );
}

function WindowResult({ window }: { window: WindowDetail }) {
  const result = window.result;
  const segments = window.source_segments ?? [];
  if (window.status === 'failed') {
    return (
      <p className="text-xs leading-5 text-red-700">
        窗口失败：{window.last_error || '未知错误'}
      </p>
    );
  }
  if (!result) {
    return <p className="text-xs text-slate-500">尚未生成结果。</p>;
  }
  const resolve = (evidenceIds: number[]): SourceSegment[] =>
    (evidenceIds ?? [])
      .map((id) => segments.find((s) => s.id === id))
      .filter((s): s is SourceSegment => Boolean(s));

  const allEntities: EvidenceSource[] = (result.entities ?? []).map((e) => ({
    id: e.id,
    label: `${e.name}${e.aliases?.length ? `（${e.aliases.join('、')}）` : ''} · ${e.kind ?? '实体'}`,
    evidence_ids: e.evidence_ids ?? [],
  }));
  const allRelations: EvidenceSource[] = (result.relations ?? []).map((r) => ({
    id: `${r.subject}|${r.predicate}|${r.object}`,
    label: `${result.entities?.find((e) => e.id === r.subject)?.name ?? r.subject} ${r.predicate ?? ''} ${result.entities?.find((e) => e.id === r.object)?.name ?? r.object}`,
    evidence_ids: r.evidence_ids ?? [],
  }));
  const allEvents: EvidenceSource[] = (result.events ?? []).map((e) => ({
    id: e.text,
    label: e.text,
    evidence_ids: e.evidence_ids ?? [],
  }));

  const showEntities = allEntities.length > 0;
  const showRelations = allRelations.length > 0;
  const showEvents = allEvents.length > 0;

  return (
    <div className="mt-2 space-y-3">
      {result.summary?.text && (
        <div>
          <p className="text-xs font-semibold text-slate-700">摘要</p>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-800">
            {result.summary.text}
          </p>
          <EvidenceBlock
            evidenceIds={result.summary.evidence_ids ?? []}
            segments={resolve(result.summary.evidence_ids ?? [])}
            title="摘要依据"
          />
        </div>
      )}
      {(showEntities || showRelations || showEvents) && (
        <div className="rounded-lg border border-fuchsia-100 bg-fuchsia-50/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold text-fuchsia-800">图谱与事件</p>
            <span className="text-[11px] text-fuchsia-700">
              {result.evidence_status === 'model_extracted_unverified'
                ? '模型抽取，尚未语义验证'
                : result.evidence_status ?? '证据状态未知'}
            </span>
          </div>
          <div className="mt-2 space-y-2">
            {allEntities.map((e) => (
              <div key={e.id}>
                <p className="text-sm text-slate-800">{e.label}</p>
                <EvidenceBlock evidenceIds={e.evidence_ids} segments={resolve(e.evidence_ids)} title="实体证据" />
              </div>
            ))}
            {allRelations.map((r) => (
              <div key={r.id}>
                <p className="text-sm text-slate-800">{r.label}</p>
                <EvidenceBlock evidenceIds={r.evidence_ids} segments={resolve(r.evidence_ids)} title="关系证据" />
              </div>
            ))}
            {allEvents.map((e) => (
              <div key={e.id}>
                <p className="text-sm text-slate-800">{e.label}</p>
                <EvidenceBlock evidenceIds={e.evidence_ids} segments={resolve(e.evidence_ids)} title="事件证据" />
              </div>
            ))}
          </div>
        </div>
      )}
      {!result.summary?.text && !showEntities && !showRelations && !showEvents && (
          <p className="text-xs text-slate-500">已分析，未提取到可靠的增强信息。</p>
        )}
    </div>
  );
}

export default function KnowledgeEnhancement({ docId }: { docId: string }) {
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [confirmCost, setConfirmCost] = useState(false);
  const [additionalCalls, setAdditionalCalls] = useState(8);
  const [error, setError] = useState('');
  const [visible, setVisible] = useState(true);
  const [currentVersion, setCurrentVersion] = useState<number | null>(null);
  const [observedAt, setObservedAt] = useState(0);
  const [jumpWindow, setJumpWindow] = useState<number | null>(null);

  const inFlight = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const fetchRuns = useCallback(
    async (signal?: AbortSignal): Promise<Run[]> => {
      const response = await fetch(
        withApiBasePath(`/api/documents/${docId}/enhancements`),
        { cache: 'no-store', credentials: 'include', signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || '读取增强记录失败');
      }
      const body = await response.json();
      if (!signal?.aborted && mountedRef.current) setCurrentVersion(body.current_version_id);
      return body.runs as Run[];
    },
    [docId],
  );

  const fetchDetail = useCallback(
    async (
      runId: number,
      pageOffset: number,
      signal?: AbortSignal,
    ): Promise<RunDetail> => {
      const response = await fetch(
        withApiBasePath(
          `/api/enhancements/${runId}?offset=${pageOffset}&limit=${PAGE_SIZE}`,
        ),
        { cache: 'no-store', credentials: 'include', signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || '读取增强结果失败');
      }
      return (await response.json()) as RunDetail;
    },
    [],
  );

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const newest = (await fetchRuns(signal)) || [];
      if (!mountedRef.current || signal?.aborted) return;
      setRuns(newest);
      setObservedAt(Date.now());
      const latest = newest[0] ?? null;
      setActiveRun(latest);
      if (latest) {
        const body = await fetchDetail(latest.id, offset, signal);
        if (body && mountedRef.current && !signal?.aborted) setDetail(body);
      } else {
        setDetail(null);
      }
    },
    [fetchRuns, fetchDetail, offset],
  );

  const activeWasRunning =
    activeRun != null && RUNNING_STATUS.includes(activeRun.status);

  useEffect(() => {
    mountedRef.current = true;
    const onVisibility = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      mountedRef.current = false;
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    const timer = window.setTimeout(() => {
      void refresh(controller.signal).catch((err) => {
        if (!controller.signal.aborted && mountedRef.current) {
          setError(err instanceof Error ? err.message : '读取增强记录失败');
        }
      });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, docId]);

  useEffect(() => {
    if (!open || !visible || !activeWasRunning || busy) return;
    let stopped = false;
    let handle = 0;
    const tick = async () => {
      if (stopped || inFlight.current) {
        handle = window.setTimeout(tick, POLL_MS);
        return;
      }
      inFlight.current = true;
      const controller = new AbortController();
      abortRef.current?.abort();
      abortRef.current = controller;
      try {
        await refresh(controller.signal);
        if (!document.hidden && !stopped && open) {
          handle = window.setTimeout(tick, POLL_MS);
        }
      } catch {
        if (!document.hidden && !stopped && open) {
          handle = window.setTimeout(tick, POLL_MS);
        }
      } finally {
        inFlight.current = false;
      }
    };
    handle = window.setTimeout(tick, POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(handle);
      abortRef.current?.abort();
    };
  }, [open, visible, activeWasRunning, docId, busy, refresh]);

  const loadRunDetail = useCallback(
    async (run: Run, pageOffset: number) => {
      setActiveRun(run);
      setOffset(pageOffset);
      const body = await fetchDetail(run.id, pageOffset);
      if (body) setDetail(body);
    },
    [fetchDetail],
  );

  const start = async () => {
    if (!mountedRef.current || !confirmCost) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch(
        withApiBasePath(`/api/documents/${docId}/enhancements`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ cost_acknowledged: true }),
        },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || '发起增强失败');
      if (!mountedRef.current) return;
      setConfirmCost(false);
      await loadRunDetail(body as Run, 0);
      const newest = (await fetchRuns()) || [];
      if (mountedRef.current) setRuns(newest);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : '发起增强失败');
      }
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!mountedRef.current || !activeRun) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch(
        withApiBasePath(`/api/enhancements/${activeRun.id}/cancel`),
        { method: 'POST', credentials: 'include' },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || '取消失败');
      if (!mountedRef.current) return;
      setActiveRun(body as Run);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : '取消失败');
      }
    } finally {
      setBusy(false);
    }
  };

  const resume = async () => {
    if (!mountedRef.current || !activeRun || !confirmCost) return;
    if (
      !Number.isInteger(additionalCalls) ||
      additionalCalls < 1 ||
      additionalCalls > MAX_BUDGET
    ) {
      setError('续跑追加调用必须在 1–32 之间；服务端会做总计上限（最多 256）校验');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response = await fetch(
        withApiBasePath(`/api/enhancements/${activeRun.id}/resume`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({
            additional_calls: additionalCalls,
            cost_acknowledged: true,
          }),
        },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || '继续处理失败');
      if (!mountedRef.current) return;
      setConfirmCost(false);
      await loadRunDetail(body as Run, offset);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : '继续处理失败');
      }
    } finally {
      setBusy(false);
    }
  };

  const goPage = async (pageOffset: number) => {
    if (!activeRun || busy) return;
    setBusy(true);
    setError('');
    try {
      const body = await fetchDetail(activeRun.id, pageOffset);
      if (!mountedRef.current) return;
      setDetail(body);
      setOffset(pageOffset);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : '读取增强结果失败');
      }
    } finally {
      setBusy(false);
    }
  };

  const handleOpenWindow = async (windowIndex: number) => {
    if (!activeRun || busy) return;
    const pageOffset = Math.floor(windowIndex / PAGE_SIZE) * PAGE_SIZE;
    setJumpWindow(windowIndex);
    if (offset !== pageOffset || !detail) await goPage(pageOffset);
    requestAnimationFrame(() => document.getElementById(
      `enhancement-window-${activeRun.id}-${windowIndex}`,
    )?.scrollIntoView({block: 'nearest', behavior: 'smooth'}));
  };

  const allowResume =
    activeRun != null && (RESUMEABLE.includes(activeRun.status) ||
      (activeRun.status === 'running' && activeRun.lease_until != null &&
        new Date(activeRun.lease_until).getTime() <= observedAt));
  const canStart =
    !activeRun ||
    activeRun.status === 'stale' ||
    activeRun.status === 'completed' ||
    (currentVersion != null && activeRun.document_version_id !== currentVersion);
  const progress =
    activeRun && activeRun.total_windows > 0
      ? Math.round((activeRun.completed_windows / activeRun.total_windows) * 100)
      : 0;
  const hasMore = detail?.next_offset != null;

  return (
    <details
      className="mt-3 rounded-xl border border-fuchsia-200 bg-white"
      open={open}
      onToggle={(event) => {
        const next = event.currentTarget.open;
        setOpen(next);
        if (next) {
          setError('');
          setOffset(0);
        }
      }}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 hover:bg-fuchsia-50">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold text-slate-800">知识增强</span>
          {activeRun ? (
            <span className="rounded-full bg-fuchsia-100 px-2 py-0.5 text-xs text-fuchsia-800">
              {RUN_LABELS[activeRun.status] ?? activeRun.status}
            </span>
          ) : runs.length > 0 ? (
            <span className="text-xs text-slate-400">未发起过增强</span>
          ) : null}
        </div>
        <span className="shrink-0 text-xs text-slate-400">展开查看</span>
      </summary>

      <div className="border-t border-fuchsia-100 p-4">
        <p className="text-sm leading-6 text-slate-600">
          基于结构化原文按窗口增量理解章节、实体关系与事件。模型输出属于解释，
          <strong className="text-slate-800">需要和原文证据核对</strong>；
          证据锚点存在不等于语义已验证。
          {runs.length === 0 &&
            ' 设置开关只对开启后的新版本生效，历史资料需要在本页手动发起。'}
          {runs.length > 0 && activeRun == null && ' 请在设置中开启后，在文档页手动发起。'}
        </p>

        {error && (
          <p role="alert" className="mt-3 text-sm text-red-700">
            {error}
          </p>
        )}

        {canStart && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/50 p-3">
            <p className="text-sm text-amber-900">
              本批仅补充章节理解、实体图谱与可选分层概览，
              不修改检索切片或向量索引；辅助切片与原子替换未开放。服务端幂等：
              当前配置与既有运行一致时复用该运行，否则以当前配置开始新运行。
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirmCost((value) => !value)}
              className="mt-2 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-sm text-amber-800 disabled:opacity-50"
            >
              {confirmCost ? '已确认费用与内容外发 ✓（点击撤销）' : '确认额外费用与内容外发'}
            </button>
            <button
              type="button"
              disabled={busy || !confirmCost}
              onClick={() => void start()}
              className="ml-2 mt-2 rounded-lg bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >
              {busy ? '处理中…' : '开始增强'}
            </button>
          </div>
        )}

        {activeRun && (
          <div className="mt-4 space-y-3">
            <div className="grid gap-2 text-xs text-slate-600 sm:grid-cols-4">
              <span>
                状态：{RUN_LABELS[activeRun.status] ?? activeRun.status}
              </span>
              <span>
                调用：{activeRun.calls_used}/{activeRun.call_budget}
              </span>
              <span>
                窗口覆盖：{activeRun.completed_windows}/{activeRun.total_windows}
                （{progress}%）
              </span>
              <span>
                原文：{activeRun.completed_source_chars}/{activeRun.total_source_chars} 字
              </span>
            </div>
            {activeRun.hierarchy?.enabled && activeRun.hierarchy.total_nodes > 0 && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-teal-700">
                <span>
                  概览节点：{activeRun.hierarchy.completed_nodes}/
                  {activeRun.hierarchy.total_nodes}（
                  {Math.round(
                    (activeRun.hierarchy.completed_nodes /
                      activeRun.hierarchy.total_nodes) *
                      100,
                  )}
                  %）
                </span>
                <span className="text-slate-400">
                  窗口覆盖与概览归纳分别统计，均不代表“全文已完整理解”。
                </span>
              </div>
            )}
            {activeRun.hierarchy?.enabled && activeRun.hierarchy.total_nodes === 0 && (
              <p className="text-xs text-teal-700">
                已启用分层概览，待窗口收束后开始归纳。
              </p>
            )}
            {activeRun.last_error && (
              <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
                {activeRun.last_error}
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              {allowResume && (
                <>
                  <label className="flex items-center gap-2 text-xs text-amber-800">
                    <input type="checkbox" checked={confirmCost} disabled={busy}
                      onChange={(event) => setConfirmCost(event.target.checked)} />
                    确认追加模型调用费用与内容外发
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={MAX_BUDGET}
                    value={additionalCalls}
                    onChange={(event) =>
                      setAdditionalCalls(Number(event.target.value) || 8)
                    }
                    disabled={busy}
                    className="w-24 rounded border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
                    aria-label="续跑追加调用预算"
                  />
                  <button
                    type="button"
                    disabled={busy || !confirmCost}
                    onClick={() => void resume()}
                    className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {confirmCost ? '确认并继续' : '继续处理（需再确认费用）'}
                  </button>
                </>
              )}
              {RUNNING_STATUS.includes(activeRun.status) && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void cancel()}
                  className="ml-auto rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                >
                  取消运行（保留已完成窗口）
                </button>
              )}
            </div>
            {allowResume && !confirmCost && (
              <p className="text-xs text-amber-700">
                续跑需要追加模型调用并重新确认费用与内容外发；每次追加 1–32，
                服务端按总计最多 256 次校验。
              </p>
            )}

            {!allowResume && RUNNING_STATUS.includes(activeRun.status) && (
              <p className="text-xs text-slate-400">处理中每 5 秒自动刷新；页面隐藏时暂停。</p>
            )}
          </div>
        )}

        {activeRun?.hierarchy?.enabled && (
          <EnhancementOverview
            key={activeRun.id}
            run={activeRun}
            visible={open && visible}
            onOpenWindow={handleOpenWindow}
          />
        )}

        {detail && (
          <div className="mt-4 space-y-3">
            {detail.windows.length === 0 && (
              <p className="text-sm text-slate-500">暂无窗口结果。</p>
            )}
            {detail.windows.map((item) => (
              <details
                key={item.ordinal}
                id={`enhancement-window-${activeRun?.id}-${item.ordinal}`}
                open={item.ordinal === jumpWindow}
                onToggle={(event) => {
                  if (event.currentTarget.open && jumpWindow !== item.ordinal) {
                    setJumpWindow(item.ordinal);
                  } else if (!event.currentTarget.open && jumpWindow === item.ordinal) {
                    setJumpWindow(null);
                  }
                }}
                className="rounded-lg border border-slate-200"
              >
                <summary className="cursor-pointer px-3 py-2 text-sm text-slate-700">
                  窗口 {item.ordinal + 1} ·{' '}
                  {item.status === 'pending' ? '待分析' : RUN_LABELS[item.status as RunStatus] ?? item.status}
                </summary>
                <div className="border-t border-slate-200 p-3">
                  <WindowResult window={item} />
                </div>
              </details>
            ))}
            <div className="flex items-center justify-between text-sm text-slate-600">
              <span>
                第 {offset + 1}–{Math.min(offset + PAGE_SIZE, detail.total_windows)} 个窗口，共{' '}
                {detail.total_windows} 个
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busy || offset === 0}
                  onClick={() => void goPage(Math.max(0, offset - PAGE_SIZE))}
                  className="rounded border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  上一页
                </button>
                <button
                  type="button"
                  disabled={busy || !hasMore}
                  onClick={() => void goPage(offset + PAGE_SIZE)}
                  className="rounded border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  下一页
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </details>
  );
}
