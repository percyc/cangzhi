'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  WEBDAV_BROWSE_PAGE_SIZE,
  WEBDAV_SEARCH_MAX_LENGTH,
  WEBDAV_SELECTION_LIMIT,
  addWebDavSelection,
  isImportableWebDavFile,
  normalizeWebDavSearch,
  removeWebDavSelection,
  requestWebDavSyncBatch,
  webDavBrowseUrl,
  webDavBreadcrumbs,
  webDavDisplayPath,
  webDavTotalPages,
} from '@/lib/webdav-import';
import type {
  WebDavBrowseFile,
  WebDavBrowsePage,
  WebDavEntryState,
  WebDavImportScope,
  WebDavSyncBatchError,
} from '@/lib/webdav-import';

const STATE_FILTERS: Array<{ value: WebDavEntryState | ''; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'discovered', label: '待同步' },
  { value: 'changed', label: '有更新' },
  { value: 'failed', label: '失败' },
  { value: 'synced', label: '已同步' },
  { value: 'suspected_missing', label: '远端缺失待确认' },
  { value: 'missing', label: '远端已确认删除' },
  { value: 'ignored', label: '已排除' },
];

const stateLabels: Record<string, string> = {
  discovered: '待同步',
  changed: '有更新',
  synced: '已同步',
  failed: '失败',
  suspected_missing: '远端缺失，待确认',
  missing: '远端已确认删除',
  ignored: '已从知识库排除',
};

export type WebDavImportProgress = {
  running: boolean;
  stopped: boolean;
  interrupted: boolean;
  label: string;
  processed: number;
  imported: number;
  updated: number;
  unchanged: number;
  failed: number;
  skipped: number;
  errors: WebDavSyncBatchError[];
  guidance: string;
};

const IDLE_PROGRESS: WebDavImportProgress = {
  running: false,
  stopped: false,
  interrupted: false,
  label: '',
  processed: 0,
  imported: 0,
  updated: 0,
  unchanged: 0,
  failed: 0,
  skipped: 0,
  errors: [],
  guidance: '',
};

/** Bounded failure list; the summary counters above stay exact. */
const WEBDAV_MAX_REPORTED_ERRORS = 20;

const RUNNING_GUIDANCE =
  '请保持本页打开：这是逐个文件的顺序导入，不是持久后台任务。离开或关闭页面会中断后续文件；已经入库的资料不会回滚，正在扫描的来源仍由后台扫描继续。';

export type WebDavBrowserProps = {
  sourceId: number;
  sourceName: string;
  disabled?: boolean;
  busy?: boolean;
  /**
   * The source card's "同步入库" asks this browser for the same bounded loop
   * over the source root, so both entry points share one controller.
   */
  fullImportRequest?: boolean;
  onFullImportHandled?: () => void;
  onRunningChange?: (running: boolean) => void;
  onImported?: () => void | Promise<void>;
  onAllowReimport?: (entry: WebDavBrowseFile) => void | Promise<void>;
  onKeepSnapshot?: (entry: WebDavBrowseFile) => void | Promise<void>;
};

type PendingConfirmation = {
  scope: WebDavImportScope;
  title: string;
  detail: string;
};

export function WebDavFileBrowser({
  sourceId,
  sourceName,
  disabled = false,
  busy = false,
  fullImportRequest = false,
  onFullImportHandled,
  onRunningChange,
  onImported,
  onAllowReimport,
  onKeepSnapshot,
}: WebDavBrowserProps) {
  const [page, setPage] = useState<WebDavBrowsePage | null>(null);
  const [directory, setDirectory] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [state, setState] = useState<WebDavEntryState | ''>('');
  const [pageNumber, setPageNumber] = useState(1);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [selected, setSelected] = useState<number[]>([]);
  const [progress, setProgress] = useState<WebDavImportProgress>(IDLE_PROGRESS);
  const [confirmation, setConfirmation] = useState<PendingConfirmation | null>(null);
  const mounted = useRef(true);
  const requestSequence = useRef(0);
  const importAbort = useRef<AbortController | null>(null);
  const runningRef = useRef(false);
  const latest = useRef({ directory, q: search, state, page: pageNumber });

  const running = progress.running;
  const scopeReady = !loading && !loadError && page !== null;
  const interactive = !disabled && !busy && !running;
  // A failed load or a directory still loading leaves rows from a stale scope on
  // screen; every mutating action stays disabled until the current scope loads.
  const actionable = interactive && scopeReady;
  const importable = useMemo(() => (page?.items ?? []).filter(isImportableWebDavFile), [page]);
  const selectedOnPage = importable.filter((item) => selected.includes(item.id)).length;
  const selectableCount = importable.length - selectedOnPage;
  const pages = webDavTotalPages(page?.total ?? 0);

  const load = useCallback(async (options: {
    directory: string | null;
    q: string;
    state: WebDavEntryState | '';
    page: number;
  }) => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setLoadError('');
    try {
      const url = webDavBrowseUrl(sourceId, {
        directory: options.directory,
        q: options.q,
        state: options.state === '' ? null : options.state,
        page: options.page,
        page_size: WEBDAV_BROWSE_PAGE_SIZE,
      });
      const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error(await readDetail(response, '文件目录读取失败'));
      const next = (await response.json()) as WebDavBrowsePage;
      if (!mounted.current || sequence !== requestSequence.current) return;
      setPage(next);
    } catch (caught) {
      if (!mounted.current || sequence !== requestSequence.current) return;
      if (caught instanceof DOMException && caught.name === 'AbortError') return;
      setLoadError(caught instanceof Error ? caught.message : '文件目录读取失败');
    } finally {
      if (mounted.current && sequence === requestSequence.current) setLoading(false);
    }
  }, [sourceId]);

  useEffect(() => {
    mounted.current = true;
    const timer = window.setTimeout(() => {
      void load({ directory, q: search, state, page: pageNumber });
    }, 0);
    return () => {
      mounted.current = false;
      importAbort.current?.abort();
      window.clearTimeout(timer);
    };
  }, [load, directory, search, state, pageNumber]);

  // The parent disables every other source action while an import is running.
  useEffect(() => {
    if (runningRef.current === running) return;
    runningRef.current = running;
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  const refresh = useCallback(
    () => load({ ...latest.current }),
    [load],
  );

  // The source card's "同步入库" lands here so the whole-source scope is
  // confirmed in this dialog and run by this same bounded loop.
  useEffect(() => {
    latest.current = { directory, q: search, state, page: pageNumber };
  }, [directory, search, state, pageNumber]);

  // Leave the request pending in the parent until the initial directory loads.
  // Consume it together with opening the dialog, not before a timer that a
  // parent rerender could cancel. Merely browsing must never open this dialog.
  useEffect(() => {
    if (!fullImportRequest || !actionable || !page) return;
    const timer = window.setTimeout(() => {
      setConfirmation({
        scope: { type: 'directory', directory: page.root },
        title: `同步入库“${sourceName}”全部待处理文件`,
        detail: '将导入来源根目录及全部子目录中的待同步、有更新和失败文件，不受当前目录、搜索词或状态筛选影响。',
      });
      onFullImportHandled?.();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fullImportRequest, actionable, page, sourceName, onFullImportHandled]);

  const reset = (next: Partial<{ directory: string | null; search: string; state: WebDavEntryState | ''; page: number }>) => {
    setPageNumber(next.page ?? 1);
    if (next.directory !== undefined) setDirectory(next.directory);
    if (next.search !== undefined) setSearch(next.search);
    if (next.state !== undefined) setState(next.state);
    // A different directory or filter invalidates ids from the previous scope.
    if (next.directory !== undefined || next.state !== undefined || next.search !== undefined) setSelected([]);
  };

  const applySearch = () => {
    const normalized = normalizeWebDavSearch(query);
    if (!normalized && query.trim()) {
      setLoadError(`搜索词最长 ${WEBDAV_SEARCH_MAX_LENGTH} 个字符。`);
      return;
    }
    setLoadError('');
    reset({ search: normalized ?? '', page: 1 });
  };

  const toggle = (id: number) => {
    setSelected((current) =>
      current.includes(id) ? removeWebDavSelection(current, id) : addWebDavSelection(current, id),
    );
  };

  const selectPage = () => {
    setSelected((current) => importable.reduce((next, item) => addWebDavSelection(next, item.id), current));
  };

  const clearSelection = () => setSelected([]);

  const requestScope = (pending: PendingConfirmation) => {
    if (!actionable) return;
    setConfirmation(pending);
  };

  const startImport = async (pending: PendingConfirmation) => {
    setConfirmation(null);
    if (!actionable) return;
    const scope = pending.scope;
    // An explicitly empty selection must never widen into a whole-directory run.
    if (scope.type === 'entries' && scope.entry_ids.length === 0) return;
    const controller = new AbortController();
    importAbort.current = controller;
    const run: WebDavImportProgress = {
      ...IDLE_PROGRESS,
      running: true,
      label: pending.title,
      guidance: RUNNING_GUIDANCE,
    };
    setProgress(run);
    const totals = { imported: 0, updated: 0, unchanged: 0, failed: 0, skipped: 0, processed: 0 };
    const errors: WebDavSyncBatchError[] = [];
    let afterId = 0;
    let throughId: number | null = null;
    let hasMore = true;
    let stopped = false;
    let interrupted = false;
    try {
      // Sequential one-file requests; the first response freezes through_id.
      while (hasMore) {
        if (controller.signal.aborted) { stopped = true; break; }
        const result = await requestWebDavSyncBatch(sourceId, scope, {
          afterId,
          throughId,
          signal: controller.signal,
        });
        if (!mounted.current) return;
        totals.imported += result.imported;
        totals.updated += result.updated;
        totals.unchanged += result.unchanged;
        totals.failed += result.failed;
        totals.skipped += result.skipped;
        totals.processed += result.processed;
        // Keep the failure list bounded while still accumulating real counts,
        // so a long run never copies an unbounded array on every request.
        for (const failure of result.errors) {
          if (errors.length >= WEBDAV_MAX_REPORTED_ERRORS) errors.shift();
          errors.push(failure);
        }
        setProgress({ ...run, ...totals, errors: [...errors] });
        const advanced = result.after_id > afterId;
        if (result.has_more && !advanced) {
          // Refuse to loop on a protocol error: without a forward cursor the
          // next request would repeat the same file forever.
          interrupted = true;
          errors.push({ entry_id: null, message: '服务未推进导入位置，已停止以避免重复处理；请重新选择剩余文件。' });
          break;
        }
        if (result.has_more && result.through_id == null) {
          interrupted = true;
          errors.push({ entry_id: null, message: '服务未返回导入范围上界，已停止；请重新选择剩余文件。' });
          break;
        }
        afterId = result.after_id;
        throughId = result.through_id ?? throughId;
        hasMore = result.has_more;
      }
    } catch (caught) {
      if (!mounted.current) return;
      // An abort is the user pressing stop (or the page going away), not a
      // failure: already imported files are intact and the run ends cleanly.
      if (controller.signal.aborted) {
        stopped = true;
      } else {
        interrupted = true;
        errors.push({
          entry_id: null,
          message: `${caught instanceof Error ? caught.message : '网络或服务异常'}（已导入的文件不会回滚，可重新选择剩余文件继续，不会自动无限重试）`,
        });
      }
    }
    importAbort.current = null;
    if (!mounted.current) return;
    const done = totals.imported + totals.updated + totals.unchanged;
    setProgress({
      ...run,
      ...totals,
      running: false,
      stopped,
      interrupted,
      errors: [...errors],
      guidance: interrupted
        ? `已中断：完成 ${totals.processed} 个文件（新增 ${totals.imported}，更新 ${totals.updated}，未变化 ${totals.unchanged}，跳过 ${totals.skipped}，失败 ${totals.failed}）。已入库 ${done} 个文件保留，可重新选择剩余文件继续。`
        : stopped
          ? `已停止：完成 ${totals.processed} 个文件（新增 ${totals.imported}，更新 ${totals.updated}，未变化 ${totals.unchanged}，跳过 ${totals.skipped}，失败 ${totals.failed}）。已入库 ${done} 个文件保留，可稍后继续。`
          : `已完成 ${totals.processed} 个文件：新增 ${totals.imported}，更新 ${totals.updated}，未变化 ${totals.unchanged}，跳过 ${totals.skipped}，失败 ${totals.failed}。资料已进入处理流程，稍后到收件箱查看解析结果。`,
    });
    if (!interrupted && !stopped) setSelected([]);
    await onImported?.();
    await refresh();
  };

  const stopImport = () => importAbort.current?.abort();
  const dismissProgress = () => setProgress(IDLE_PROGRESS);

  const runEntryAction = async (action: ((entry: WebDavBrowseFile) => void | Promise<void>) | undefined, entry: WebDavBrowseFile) => {
    if (!action || !actionable) return;
    await action(entry);
    // The paged browser, not the removed flat entry list, is the source of truth.
    await refresh();
  };

  const retryVisibleFailures = () => {
    if (!page) return;
    requestScope({
      scope: { type: 'failed', directory: page.directory },
      title: '重试当前目录的失败文件',
      detail: `只重试 ${webDavDisplayPath(page.root, page.directory) || '当前目录'} 及其子目录中的失败文件（当前范围失败 ${page.failed_count} 个），不包含待同步、有更新或已同步的文件。`,
    });
  };

  const crumbs = page ? webDavBreadcrumbs(page.root, page.directory, page.root) : [];

  return (
    <section className="mt-4 border-t border-slate-100 pt-4" aria-label={`${sourceName} 文件目录`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-900">文件目录与选择导入</h3>
          <p className="mt-1 text-xs text-slate-500">
            按目录分页浏览远端文件清单，可勾选待同步或失败的文件逐个导入。目录来自上次扫描结果，不会重新扫描远端。
          </p>
        </div>
        <button
          type="button"
          disabled={!interactive}
          onClick={() => void refresh()}
          className="rounded-xl border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 disabled:opacity-40"
        >
          {loading ? '正在刷新…' : '刷新目录'}
        </button>
      </div>

      <nav aria-label="目录路径" className="mt-3 flex flex-wrap items-center gap-1 text-xs text-slate-600">
        {crumbs.map((crumb, index) => (
          <span key={crumb.path} className="flex items-center gap-1">
            {index > 0 && <span aria-hidden="true" className="text-slate-300">/</span>}
            <button
              type="button"
              disabled={!actionable}
              onClick={() => reset({ directory: crumb.path, page: 1 })}
              className="max-w-[12rem] truncate rounded px-1 py-0.5 text-left hover:bg-slate-100 disabled:opacity-40"
              title={crumb.path}
            >
              {index === 0 ? `根目录 ${crumb.name}` : crumb.name}
            </button>
          </span>
        ))}
        {page?.parent && (
          <button
            type="button"
            disabled={!actionable}
            onClick={() => reset({ directory: page.parent, page: 1 })}
            className="ml-1 rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
          >
            返回上级
          </button>
        )}
      </nav>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-600">
          搜索文件名
          <input
            value={query}
            maxLength={WEBDAV_SEARCH_MAX_LENGTH + 20}
            disabled={!interactive}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); applySearch(); } }}
            placeholder="按文件名包含匹配"
            className="mt-1 block w-56 rounded border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-100"
          />
        </label>
        <button
          type="button"
          disabled={!interactive}
          onClick={applySearch}
          className="rounded-xl border border-slate-300 px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          搜索
        </button>
        {search && (
          <button
            type="button"
            disabled={!interactive}
            onClick={() => { setQuery(''); reset({ search: '' }); }}
            className="rounded-xl px-2 py-1.5 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
          >
            清除搜索
          </button>
        )}
        <label className="text-xs text-slate-600">
          状态
          <select
            value={state}
            disabled={!interactive}
            onChange={(event) => reset({ state: event.target.value as WebDavEntryState | '', page: 1 })}
            className="mt-1 block rounded border border-slate-300 bg-white px-2 py-1.5 text-sm disabled:bg-slate-100"
          >
            {STATE_FILTERS.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>
      </div>

      {search && (
        <p className="mt-2 text-xs text-slate-500">
          搜索“{search}”会同时匹配子目录里的文件；导入范围仍以下方确认的范围为准，不会因为搜索而扩大。
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <button
          type="button"
          disabled={!actionable || selectableCount === 0}
          onClick={selectPage}
          className="rounded-xl border border-slate-300 px-3 py-1.5 font-medium disabled:opacity-40"
        >
          选中本页可导入文件
        </button>
        <button
          type="button"
          disabled={!actionable || selected.length === 0}
          onClick={clearSelection}
          className="rounded-xl px-2 py-1.5 text-slate-600 hover:bg-slate-100 disabled:opacity-40"
        >
          清空选择
        </button>
        <button
          type="button"
          disabled={!actionable || selected.length === 0}
          onClick={() => requestScope({
            scope: { type: 'entries', entry_ids: [...selected] },
            title: `导入已选中的 ${selected.length} 个文件`,
            detail: `只导入当前勾选的 ${selected.length} 个文件，不会包含同目录其他文件，也不会按目录扩展。`,
          })}
          className="rounded-xl bg-slate-950 px-3 py-1.5 font-medium text-white disabled:opacity-40"
        >
          导入选中（{selected.length}）
        </button>
        <button
          type="button"
          disabled={!actionable || !page || page.pending_count + page.failed_count === 0}
          onClick={() => page && requestScope({
            scope: { type: 'directory', directory: page.directory },
            title: '导入当前目录（含所有子目录）',
            detail: `导入 ${webDavDisplayPath(page.root, page.directory) || '当前目录'} 及其全部子目录中的待同步、更新和失败文件；不受上方搜索词与状态筛选影响，待同步 ${page.pending_count} 个、失败 ${page.failed_count} 个（含子目录）。`,
          })}
          className="rounded-xl border border-slate-300 px-3 py-1.5 font-medium disabled:opacity-40"
        >
          导入当前目录
        </button>
        <button
          type="button"
          disabled={!actionable || !page || page.failed_count === 0}
          onClick={retryVisibleFailures}
          className="rounded-xl border border-slate-300 px-3 py-1.5 font-medium disabled:opacity-40"
        >
          重试本目录失败文件
        </button>
        <span className="text-slate-500">
          已选 {selected.length} / 最多 {WEBDAV_SELECTION_LIMIT}；上方“同步入库”按整个来源范围确认
        </span>
      </div>

      {running && (
        <div role="status" aria-live="polite" className="mt-3 rounded-xl bg-slate-50 p-3 text-xs text-slate-700">
          <p className="text-sm font-semibold">{progress.label}</p>
          <p className="mt-1">
            已处理 {progress.processed} 个：新增 {progress.imported}，更新 {progress.updated}，未变化 {progress.unchanged}，跳过 {progress.skipped}，失败 {progress.failed}
          </p>
          <p className="mt-1 leading-5">{progress.guidance}</p>
          <p className="mt-1 text-slate-500">导入进行中，编辑配置、删除连接器、启停、扫描和目录切换已暂时停用。</p>
          <button type="button" onClick={stopImport} className="mt-2 rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium">
            停止后续文件
          </button>
        </div>
      )}

      {!running && (progress.processed > 0 || progress.errors.length > 0 || progress.stopped || progress.interrupted) && (
        <div
          role="status"
          aria-live="polite"
          className={`mt-3 rounded-xl p-3 text-xs ${progress.interrupted ? 'bg-red-50 text-red-800' : 'bg-slate-50 text-slate-700'}`}
        >
          <p className="text-sm font-semibold">{progress.label}</p>
          <p className="mt-1">
            已处理 {progress.processed} 个：新增 {progress.imported}，更新 {progress.updated}，未变化 {progress.unchanged}，跳过 {progress.skipped}，失败 {progress.failed}
          </p>
          <p className="mt-1 leading-5">{progress.guidance}</p>
          {progress.failed > progress.errors.length && (
            <p className="mt-1 text-slate-500">
              仅显示最近 {WEBDAV_MAX_REPORTED_ERRORS} 条失败原因，失败总数以“失败 {progress.failed}”为准；完整原因可在文件目录中逐个查看。
            </p>
          )}
          {progress.errors.length > 0 && (
            <ul className="mt-2 max-h-32 space-y-1 overflow-y-auto">
              {progress.errors.map((item, index) => (
                <li key={`${item.entry_id ?? 'batch'}-${index}`} className="break-all">
                  {item.entry_id ? `文件 ${item.entry_id}：` : ''}{item.message}
                </li>
              ))}
            </ul>
          )}
          <button type="button" onClick={dismissProgress} className="mt-2 rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium">
            收起结果
          </button>
        </div>
      )}

      {loadError && (
        <div className="mt-3 rounded-xl bg-red-50 p-3 text-xs text-red-800" role="alert">
          <p>{loadError}</p>
          <p className="mt-1 text-red-700">当前显示的仍是上一次成功读取的内容，导入与目录切换已停用，避免按过期目录提交请求。</p>
          <button
            type="button"
            disabled={loading}
            onClick={() => void refresh()}
            className="mt-2 rounded-lg border border-red-300 bg-white px-3 py-1.5 font-medium"
          >
            重试读取
          </button>
        </div>
      )}

      <div className="mt-3 max-h-96 overflow-y-auto overflow-x-auto rounded-xl border border-slate-200">
        <table className="min-w-full text-left text-xs">
          <thead className="bg-slate-50 text-slate-500">
            <tr>
              <th className="w-8 py-2 pl-3">
                <input
                  type="checkbox"
                  aria-label="选中本页可导入文件"
                  disabled={!actionable || importable.length === 0}
                  checked={importable.length > 0 && selectedOnPage === importable.length}
                  onChange={(event) => (event.target.checked ? selectPage() : clearSelection())}
                />
              </th>
              <th className="py-2">名称</th>
              <th className="py-2">状态</th>
              <th className="py-2">大小</th>
              <th className="py-2 pr-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {(page?.items ?? []).map((item) =>
              item.kind === 'folder' ? (
                <tr key={`folder:${item.remote_path}`} className="border-t">
                  <td className="py-2 pl-3" />
                  <td className="py-2 pr-3">
                    <button
                      type="button"
                      disabled={!actionable}
                      onClick={() => reset({ directory: item.remote_path, page: 1 })}
                      className="text-left font-medium text-blue-700 hover:underline disabled:opacity-40"
                    >
                      📁 {item.name}
                    </button>
                    <span className="ml-2 text-slate-500">
                      {item.file_count} 个文件 · 待同步 {item.pending_count} · 失败 {item.failed_count}
                    </span>
                  </td>
                  <td className="text-slate-500">目录</td>
                  <td className="text-slate-500">—</td>
                  <td className="py-2 pr-3 text-slate-400">进入目录</td>
                </tr>
              ) : (
                <FileRow
                  key={item.id}
                  item={item}
                  selected={selected.includes(item.id)}
                  actionable={actionable}
                  onToggle={toggle}
                  onAllowReimport={(entry) => runEntryAction(onAllowReimport, entry)}
                  onKeepSnapshot={(entry) => runEntryAction(onKeepSnapshot, entry)}
                />
              ),
            )}
            {page && page.items.length === 0 && !loading && !loadError && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-slate-500">
                  {search
                    ? `没有匹配“${search}”的文件。搜索会同时查找子目录。`
                    : '此目录没有已扫描到的文件。请先执行“扫描文件”，再回到这里选择导入。'}
                </td>
              </tr>
            )}
            {(!page || page.items.length === 0) && loading && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-slate-500">正在读取文件目录…</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        <span>
          {page
            ? `第 ${page.page} / ${pages} 页 · 共 ${page.total} 项 · 当前目录待同步 ${page.pending_count} · 失败 ${page.failed_count}`
            : '尚未读取目录'}
        </span>
        <span className="flex items-center gap-2">
          <button
            type="button"
            disabled={!actionable || pageNumber <= 1}
            onClick={() => setPageNumber((current) => Math.max(1, current - 1))}
            className="rounded-lg border border-slate-300 px-2 py-1 disabled:opacity-40"
          >
            上一页
          </button>
          <button
            type="button"
            disabled={!actionable || pageNumber >= pages}
            onClick={() => setPageNumber((current) => current + 1)}
            className="rounded-lg border border-slate-300 px-2 py-1 disabled:opacity-40"
          >
            下一页
          </button>
        </span>
      </div>

      {confirmation && (
        <ImportConfirmationDialog
          confirmation={confirmation}
          onCancel={() => setConfirmation(null)}
          onConfirm={() => void startImport(confirmation)}
        />
      )}
    </section>
  );
}

function ImportConfirmationDialog({
  confirmation,
  onCancel,
  onConfirm,
}: {
  confirmation: PendingConfirmation;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4">
      <div role="dialog" aria-modal="true" aria-label={confirmation.title} className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
        <h2 className="text-lg font-semibold text-slate-900">{confirmation.title}</h2>
        <p className="mt-2 text-sm leading-6 text-slate-600">{confirmation.detail}</p>
        <p className="mt-3 rounded-xl bg-amber-50 p-3 text-xs leading-5 text-amber-800">{RUNNING_GUIDANCE}</p>
        <div className="mt-6 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="rounded-xl border border-slate-300 px-4 py-2 text-sm">取消</button>
          <button type="button" onClick={onConfirm} className="rounded-xl bg-slate-950 px-4 py-2 text-sm text-white">确认导入</button>
        </div>
      </div>
    </div>
  );
}

function FileRow({
  item,
  selected,
  actionable,
  onToggle,
  onAllowReimport,
  onKeepSnapshot,
}: {
  item: WebDavBrowseFile;
  selected: boolean;
  actionable: boolean;
  onToggle: (id: number) => void;
  onAllowReimport: (entry: WebDavBrowseFile) => void | Promise<void>;
  onKeepSnapshot: (entry: WebDavBrowseFile) => void | Promise<void>;
}) {
  const importable: boolean = isImportableWebDavFile(item);
  const keepable = Boolean(
    item.document_id
    && !item.keep_snapshot
    && (item.state === 'missing' || (item.state === 'ignored' && item.ignore_reason === 'document_trashed')),
  );
  return (
    <tr className="border-t align-top">
      <td className="py-2 pl-3">
        <input
          type="checkbox"
          aria-label={`选择 ${item.name}`}
          disabled={!actionable || !importable}
          checked={selected}
          onChange={() => onToggle(item.id)}
        />
      </td>
      <td className="max-w-sm break-all py-2 pr-3">
        <span className="font-medium text-slate-800">{item.name}</span>
        <span className="mt-0.5 block text-[11px] text-slate-400">{item.remote_path}</span>
        {item.last_error && (
          <span className="mt-1 block text-[11px] leading-4 text-red-700">失败原因：{item.last_error}</span>
        )}
      </td>
      <td className="py-2 text-slate-600">
        {stateLabels[item.state] ?? item.state}
        {item.keep_snapshot ? ' · 已保留快照' : ''}
        {item.ignore_reason === 'permanent_deleted' ? ' · 已永久排除' : ''}
        {item.state === 'ignored' && item.ignore_reason === 'document_trashed' && item.resume_state === 'missing'
          ? ' · 远端已确认删除，已入回收站'
          : ''}
        {item.missing_count > 0 && item.state === 'suspected_missing' ? ` · 缺失确认 ${item.missing_count} 次` : ''}
      </td>
      <td className="py-2 text-slate-600">{formatSize(item.file_size)}</td>
      <td className="py-2 pr-3">
        <div className="flex flex-wrap items-center gap-3">
          {item.document_id && (
            <Link className="text-blue-700 hover:underline" href={`/documents/${item.document_id}`}>查看</Link>
          )}
          {keepable && (
            <button type="button" disabled={!actionable} onClick={() => void onKeepSnapshot(item)} className="text-blue-700 hover:underline disabled:opacity-40">
              保留知识快照
            </button>
          )}
          {item.ignore_reason === 'permanent_deleted' && (
            <button type="button" disabled={!actionable} onClick={() => void onAllowReimport(item)} className="text-blue-700 hover:underline disabled:opacity-40">
              允许重新入库
            </button>
          )}
          {!importable && !item.document_id && item.ignore_reason !== 'permanent_deleted' ? '—' : null}
        </div>
      </td>
    </tr>
  );
}

function formatSize(value: number | null) {
  if (value == null) return '—';
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function readDetail(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string' && body.detail) return body.detail;
    if (body.detail && typeof body.detail === 'object' && 'message' in body.detail) {
      const message = (body.detail as { message?: unknown }).message;
      if (typeof message === 'string' && message) return message;
    }
    return fallback;
  } catch {
    return fallback;
  }
}
