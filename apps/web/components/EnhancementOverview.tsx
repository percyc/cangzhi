'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { withApiBasePath } from '@/lib/paths';

type Hierarchy = {
  enabled: boolean;
  status: string;
  total_nodes: number;
  completed_nodes: number;
  root_key: string | null;
};

type RunStatus =
  | 'queued'
  | 'running'
  | 'partial'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'stale';

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

const RUNNING: RunStatus[] = ['queued', 'running'];
const CHILD_MAX = 4;

export type OverviewChild = {
  ref: string;
  kind: 'window' | 'node';
  window_index: number | null;
  node_key: string | null;
  status: string;
  summary: { text: string; evidence_ids?: number[]; support_refs?: string[] };
  entities: Array<{ id: string; name: string; kind: string }>;
};

export type OverviewNode = {
  node_key: string;
  level: number;
  status: string;
  window_start: number;
  window_stop: number;
  result: {
    summary: { text: string; support_refs: string[] };
    evidence_status?: string;
  } | null;
  children: OverviewChild[];
  evidence_status: string;
};

export type OverviewResponse = {
  run: Run;
  enabled: boolean;
  node: OverviewNode | null;
  read_only: true;
  model_calls: 0;
};

type Props = {
  run: Run;
  visible: boolean;
  onOpenWindow: (windowIndex: number) => void;
};

const NODE_LABELS: Record<string, string> = {
  pending: '待归纳',
  running: '归纳中',
  failed: '归纳失败',
  completed: '已完成',
};

function childLabel(child: OverviewChild) {
  if (child.kind === 'window') {
    return `窗口 ${(child.window_index ?? 0) + 1}`;
  }
  return `子概览 ${child.node_key ?? ''}`;
}

function childStatus(child: OverviewChild) {
  if (child.kind === 'window') {
    return child.status === 'completed' ? '已完成' : child.status;
  }
  return NODE_LABELS[child.status] ?? child.status;
}

export default function EnhancementOverview({
  run,
  visible,
  onOpenWindow,
}: Props) {
  const hierarchy = run.hierarchy;
  const [expanded, setExpanded] = useState(false);
  const [stack, setStack] = useState<string[]>([]);
  const [response, setResponse] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<{key: string; message: string} | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [shownRunId, setShownRunId] = useState<number | null>(null);

  const inFlight = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current?.abort();
    };
  }, []);

  const fetchNode = useCallback(
    async (nodeKey: string | null, signal: AbortSignal) => {
      const query = nodeKey ? `?node_key=${encodeURIComponent(nodeKey)}` : '';
      const response = await fetch(
        withApiBasePath(`/api/enhancements/${run.id}/overview${query}`),
        { cache: 'no-store', credentials: 'include', signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || '读取分层概览失败');
      }
      return (await response.json()) as OverviewResponse;
    },
    [run.id],
  );

  const currentKey = stack[stack.length - 1] ?? null;
  const progressKey = `${run.id}:${run.status}:${run.calls_used}:${hierarchy.status}:${hierarchy.completed_nodes}/${hierarchy.total_nodes}`;
  const requestKey = `${progressKey}:${currentKey ?? 'root'}`;
  const currentResponse = loadedKey === requestKey ? response : null;
  const currentError = error?.key === requestKey ? error.message : null;
  const reading =
    expanded &&
    visible &&
    hierarchy.total_nodes > 0 &&
    currentResponse === null &&
    currentError === null;

  useEffect(() => {
    return () => inFlight.current?.abort();
  }, [run.id]);

  useEffect(() => {
    if (!expanded || !visible) return;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    fetchNode(currentKey, controller.signal)
      .then((body) => {
        if (!controller.signal.aborted && mounted.current) {
          setResponse(body);
          setLoadedKey(requestKey);
          setError(null);
          setShownRunId(body.run?.id ?? run.id);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted && mounted.current) {
          setError({key: requestKey, message: err instanceof Error ? err.message : '读取分层概览失败'});
        }
      });
    return () => controller.abort();
  }, [expanded, visible, requestKey, currentKey, fetchNode, run.id]);

  const nowRunning = RUNNING.includes(run.status);
  const overviewPending =
    hierarchy.enabled && hierarchy.total_nodes > 0 &&
    hierarchy.completed_nodes < hierarchy.total_nodes;

  if (!hierarchy.enabled) return null;

  const node = currentResponse?.node ?? null;
  const overviewEnabled = currentResponse?.enabled ?? hierarchy.enabled;
  const overviewProgress =
    hierarchy.total_nodes > 0
      ? Math.round((hierarchy.completed_nodes / hierarchy.total_nodes) * 100)
      : 0;
  const nodeChildren = node?.children ?? [];
  const childrenShown = nodeChildren.slice(0, CHILD_MAX);
  const truncated = nodeChildren.length > CHILD_MAX;

  const openChild = (child: OverviewChild) => {
    if (child.kind === 'window') {
      if (child.window_index != null) onOpenWindow(child.window_index);
      return;
    }
    if (child.node_key) {
      setStack((current) => [...current, child.node_key as string]);
    }
  };

  return (
    <div className="mt-4 rounded-xl border border-teal-200 bg-white">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        className="flex w-full list-none items-center justify-between gap-3 px-4 py-3 text-left hover:bg-teal-50"
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-slate-800">
            分层概览（树形汇总）
          </span>
          <span className="rounded-full bg-teal-100 px-2 py-0.5 text-xs text-teal-800">
            {run.status === 'stale' ? '原文已变化' : hierarchy.status === 'completed' ? '已生成' :
              nowRunning ? '处理中' : overviewPending ? '尚未完成' : '未生成'}
          </span>
        </span>
        <span className="shrink-0 text-xs text-slate-400">
          {expanded ? '收起' : '展开'}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-teal-100 p-4">
          <p className="text-xs leading-5 text-slate-500">
            局部窗口分析完成后逐层向上归纳，与窗口分析共享同一份调用预算。
            树形概览是模型解释，<strong className="text-slate-700">不代表全文已完整理解</strong>；
            覆盖范围以窗口进度为准，归纳进度只反映已生成的汇总节点。
            同名实体在各窗口保持独立身份，不自动合并。
            分组依据连续窗口，不等同于文档真实章节。
          </p>

          {!overviewEnabled && (
            <p className="mt-3 rounded border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
              当前运行未启用分层概览模块。
            </p>
          )}

          {overviewEnabled && hierarchy.total_nodes === 0 && (
            <p className="mt-3 text-sm text-slate-500">尚未生成概览节点。</p>
          )}

          {overviewEnabled && hierarchy.total_nodes > 0 && (
            <>
              <div className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                <span>
                  概览节点：{hierarchy.completed_nodes}/{hierarchy.total_nodes}（{overviewProgress}%）
                </span>
                <span>
                  窗口覆盖：{run.completed_windows}/{run.total_windows}（
                  {run.total_windows > 0 ? Math.round((run.completed_windows / run.total_windows) * 100) : 0}
                  %）
                </span>
              </div>
              {overviewPending && (
                <p className="mt-2 text-xs text-slate-400">
                  窗口与概览共用预算，归纳在对应窗口收束后推进。
                </p>
              )}
            </>
          )}

          {!node && stack.length > 0 && (
            <button type="button" onClick={() => setStack((current) => current.slice(0, -1))}
              className="mt-3 rounded border px-2 py-1 text-xs">返回上一层</button>
          )}
          {currentError && (
            <p role="alert" className="mt-3 text-sm text-red-700">
              {currentError}
            </p>
          )}
          {reading && (
            <p className="mt-3 text-sm text-slate-500">正在读取概览…</p>
          )}

          {shownRunId != null && shownRunId !== run.id && node == null && !reading && (
            <p className="mt-3 text-sm text-amber-700">
              已切换运行，概览已重置，请重新展开读取。
            </p>
          )}

          {node && (
            <div className="mt-3 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={stack.length === 0}
                  onClick={() => setStack([])}
                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-40"
                >
                  根节点
                </button>
                <button
                  type="button"
                  disabled={stack.length === 0}
                  onClick={() => setStack((current) => current.slice(0, -1))}
                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-40"
                >
                  返回上一层
                </button>
                <span className="text-xs text-slate-400">
                  路径：/ {stack.join(' / ') || '根'}
                </span>
              </div>

              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="text-xs font-semibold text-slate-700">
                  节点 {node.node_key} · 层级 {node.level} · 窗口 {node.window_start + 1}–{node.window_stop}
                  {' · '}{NODE_LABELS[node.status] ?? node.status}
                </p>
                {node.result?.summary?.text ? (
                  <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-800">
                    {node.result.summary.text}
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-slate-500">
                    {node.status === 'completed' ? '已分析，未提取到可归纳信息。' : '该节点尚未生成摘要。'}
                  </p>
                )}
                <p className="mt-1 text-[11px] text-slate-400">
                  证据状态：{node.evidence_status ?? 'model_extracted_unverified'}
                </p>
              </div>

              {childrenShown.map((child) => (
                <div
                  key={child.ref}
                  className="rounded-lg border border-slate-200 p-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-medium text-slate-800">
                      {childLabel(child)} · {childStatus(child)}
                      {node.result?.summary.support_refs.includes(child.ref) ? ' · 本概览的依据' : ''}
                    </span>
                    <button
                      type="button"
                      onClick={() => openChild(child)}
                      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {child.kind === 'window' ? '查看窗口证据' : '下钻查看'}
                    </button>
                  </div>
                  {child.summary?.text ? (
                    <p className="mt-1 text-xs leading-5 text-slate-600">
                      {child.summary.text}
                    </p>
                  ) : (
                    <p className="mt-1 text-xs text-slate-400">摘要待生成。</p>
                  )}
                  {child.kind === 'window' &&
                    (child.summary?.evidence_ids?.length ?? 0) > 0 && (
                    <p className="mt-1 text-[11px] text-amber-700">
                      原文证据 {child.summary?.evidence_ids?.length} 处（窗口内编号）
                    </p>
                  )}
                  {(child.entities?.length ?? 0) > 0 && child.kind === 'window' && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {child.entities.map((entity) => (
                        <span
                          key={entity.id}
                          className="rounded-full border border-fuchsia-200 bg-fuchsia-50 px-2 py-0.5 text-[11px] text-fuchsia-800"
                        >
                          {entity.name}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="mt-1 text-[11px] text-slate-400">
                    同名实体不自动合并，各窗口独立身份；有证据锚点不等于语义已验证。
                  </p>
                </div>
              ))}
              {truncated && (
                <p className="text-xs text-slate-400">
                  单次最多展示 {CHILD_MAX} 个子节点，其余请逐层下钻。
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
