// Pure request/contract helpers for the paged WebDAV file browser.
//
// Every batch is one HTTP request that downloads at most `limit` files
// sequentially on the server. The browser never downloads remote bytes and
// never fetches the whole inventory: folders, rows and totals come from the
// server's paged browse endpoint. This module holds only URL/body/validation
// logic so it can be regression tested without a browser, API or credentials.

/** Server-side page size for the paged browse list. */
export const WEBDAV_BROWSE_PAGE_SIZE = 25;

/** Hard cap on one explicitly selected import request. */
export const WEBDAV_SELECTION_LIMIT = 1000;

/** Files per sequential sync-batch request; the server processes them serially. */
export const WEBDAV_IMPORT_BATCH_LIMIT = 1;

/** Server maximum for the literal filename search term. */
export const WEBDAV_SEARCH_MAX_LENGTH = 200;

export const WEBDAV_ENTRY_STATES = [
  'discovered',
  'changed',
  'failed',
  'synced',
  'ignored',
  'suspected_missing',
  'missing',
] as const;

export type WebDavEntryState = (typeof WEBDAV_ENTRY_STATES)[number];

export type WebDavBrowseFolder = {
  kind: 'folder';
  name: string;
  remote_path: string;
  file_count: number;
  pending_count: number;
  failed_count: number;
};

export type WebDavBrowseFile = {
  kind: 'file';
  id: number;
  name: string;
  remote_path: string;
  file_size: number | null;
  state: string;
  document_id: number | null;
  last_error: string | null;
  ignore_reason: string | null;
  resume_state: string | null;
  missing_count: number;
  keep_snapshot: boolean;
};

export type WebDavBrowseItem = WebDavBrowseFolder | WebDavBrowseFile;

export type WebDavBrowsePage = {
  root: string;
  directory: string;
  parent: string | null;
  page: number;
  page_size: number;
  total: number;
  items: WebDavBrowseItem[];
  pending_count: number;
  failed_count: number;
};

export type WebDavBrowseQuery = {
  directory?: string | null;
  q?: string;
  state?: WebDavEntryState | null;
  recursive?: boolean;
  page?: number;
  page_size?: number;
};

/**
 * Build the paged browse URL. `directory` is optional: omitting it lets the
 * server resolve the source root. A search term implies recursive matching.
 */
export function webDavBrowseUrl(sourceId: number, query: WebDavBrowseQuery = {}): string {
  const search = query.q?.trim() ? query.q.trim() : '';
  const params = new URLSearchParams();
  if (query.directory) params.set('directory', query.directory);
  if (search) params.set('q', search);
  if (query.state) params.set('state', query.state);
  // A filename search spans descendants; keep the explicit flag otherwise.
  if (query.recursive || search) params.set('recursive', 'true');
  if (query.page && query.page > 1) params.set('page', String(query.page));
  params.set('page_size', String(query.page_size ?? WEBDAV_BROWSE_PAGE_SIZE));
  return `/api/webdav/${sourceId}/browse?${params.toString()}`;
}

/** Explicitly selected files and directory/failed scopes are mutually exclusive. */
export type WebDavImportScope =
  | { type: 'entries'; entry_ids: number[] }
  | { type: 'directory'; directory: string }
  | { type: 'failed'; directory: string };

export type WebDavSyncBatchBody = {
  entry_ids?: number[];
  directory?: string;
  failed_only?: boolean;
  after_id: number;
  through_id?: number;
  limit: number;
};

export type WebDavSyncBatchError = { entry_id: number | null; message: string };

export type WebDavSyncBatchResult = {
  ok: boolean;
  imported: number;
  updated: number;
  unchanged: number;
  failed: number;
  skipped: number;
  processed: number;
  after_id: number;
  through_id: number | null;
  has_more: boolean;
  errors: WebDavSyncBatchError[];
};

export type WebDavImportRequest = {
  /** The first response freezes the batch upper bound; later calls reuse it. */
  throughId?: number | null;
  afterId?: number;
  limit?: number;
  signal?: AbortSignal;
};

/**
 * One sequential sync-batch request. Per-file failures are reported by the
 * server and still advance the cursor; only a transport/HTTP rejection throws,
 * so the caller can stop without losing already imported work.
 */
export async function requestWebDavSyncBatch(
  sourceId: number,
  scope: WebDavImportScope,
  request: WebDavImportRequest = {},
  fetcher: typeof fetch = fetch,
): Promise<WebDavSyncBatchResult> {
  const limit = request.limit ?? WEBDAV_IMPORT_BATCH_LIMIT;
  const body: WebDavSyncBatchBody = {
    after_id: request.afterId ?? 0,
    limit,
  };
  if (request.throughId !== undefined && request.throughId !== null) {
    body.through_id = request.throughId;
  }
  if (scope.type === 'entries') {
    // An explicit empty list selects nothing; never fall back to a whole scope.
    body.entry_ids = scope.entry_ids;
  } else if (scope.type === 'failed') {
    body.directory = scope.directory;
    body.failed_only = true;
  } else {
    body.directory = scope.directory;
  }
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  if (request.signal) init.signal = request.signal;
  const response = await fetcher(`/api/webdav/${sourceId}/sync-batch`, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(webDavSyncBatchErrorMessage(payload));
  }
  return normalizeWebDavSyncBatch(payload);
}

export function normalizeWebDavSyncBatch(payload: unknown): WebDavSyncBatchResult {
  const body = (payload ?? {}) as Partial<WebDavSyncBatchResult>;
  const count = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
  return {
    ok: body.ok === true,
    imported: count(body.imported),
    updated: count(body.updated),
    unchanged: count(body.unchanged),
    failed: count(body.failed),
    skipped: count(body.skipped),
    processed: count(body.processed),
    after_id: count(body.after_id),
    through_id: typeof body.through_id === 'number' && Number.isFinite(body.through_id) ? body.through_id : null,
    has_more: body.has_more === true,
    errors: Array.isArray(body.errors)
      ? body.errors.flatMap((item) => {
          const entry = (item ?? {}) as Partial<WebDavSyncBatchError>;
          const message = typeof entry.message === 'string' && entry.message ? entry.message : '同步失败';
          return [{ entry_id: typeof entry.entry_id === 'number' ? entry.entry_id : null, message }];
        })
      : [],
  };
}

function webDavSyncBatchErrorMessage(payload: unknown): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail) return detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string' && message) return message;
    }
  }
  return '同步请求失败，已导入的文件不会回滚';
}

export function webDavTotalPages(total: number, pageSize = WEBDAV_BROWSE_PAGE_SIZE): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / Math.max(1, pageSize)));
}

/** Only pending or failed files can be (re)imported from a selection. */
export function isImportableWebDavFile(item: WebDavBrowseItem): item is WebDavBrowseFile {
  return item.kind === 'file' && (item.state === 'discovered' || item.state === 'changed' || item.state === 'failed');
}

/**
 * Merge a selection. Adding is capped at {@link WEBDAV_SELECTION_LIMIT}; the
 * existing selection is never silently replaced or extended beyond the cap.
 */
export function addWebDavSelection(selected: number[], id: number, limit = WEBDAV_SELECTION_LIMIT): number[] {
  if (selected.includes(id) || selected.length >= limit) return selected;
  return [...selected, id];
}

export function removeWebDavSelection(selected: number[], id: number): number[] {
  return selected.filter((entry) => entry !== id);
}

/** Search terms longer than the server bound are refused, never silently cut. */
export function normalizeWebDavSearch(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  return trimmed.length > WEBDAV_SEARCH_MAX_LENGTH ? null : trimmed;
}

export type WebDavPathCrumb = { name: string; path: string };

/** Human readable breadcrumb; the scope root is labelled by its own name. */
export function webDavBreadcrumbs(root: string, directory: string, rootLabel: string): WebDavPathCrumb[] {
  const crumbs: WebDavPathCrumb[] = [{ name: rootLabel, path: root }];
  if (directory === root) return crumbs;
  const prefix = root.endsWith('/') ? root : `${root}/`;
  if (!directory.startsWith(prefix)) return crumbs;
  let current = root === '/' ? '' : root;
  for (const segment of directory.slice(prefix.length).split('/')) {
    if (!segment) continue;
    current = `${current}/${segment}`;
    crumbs.push({ name: segment, path: current });
  }
  return crumbs;
}

/** Short display form for a remote path; the browser shows full paths in rows. */
export function webDavDisplayPath(root: string, path: string): string {
  const prefix = root.endsWith('/') ? root : `${root}/`;
  return path.startsWith(prefix) ? path.slice(prefix.length) || path : path;
}