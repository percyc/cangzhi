// Only note text is permitted here. Never persist API keys, cookies or form state.
export const NOTE_DRAFT_TTL_MS = 24 * 60 * 60 * 1000;
export const NOTE_DRAFT_MAX_BYTES = 512 * 1024;
const PREFIX = 'cangzhi:note-draft:v1:';

export type NoteDraft = { title: string; content: string; baseVersion: number | null };
export type NoteDraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
export type DraftWriteStatus = 'idle' | 'saved' | 'unavailable' | 'too-large' | 'expired';
export type DraftReadResult = {
  draft: NoteDraft | null;
  savedAt: number | null;
  status: 'empty' | 'restored' | 'expired' | 'invalid' | 'unavailable';
};

export function noteWorkspace(cookie: string): string {
  const raw = cookie.split(';').map((part) => part.trim())
    .find((part) => part.startsWith('cangzhi_workspace='))?.slice('cangzhi_workspace='.length);
  try {
    const workspace = raw ? decodeURIComponent(raw) : 'default';
    return /^[a-z0-9][a-z0-9-]{0,63}$/.test(workspace) ? workspace : 'default';
  } catch { return 'default'; }
}

export function noteDraftKey(workspace: string, target: string): string {
  return `${PREFIX}${encodeURIComponent(workspace)}:${encodeURIComponent(target)}`;
}

export function browserDraftStorage(): NoteDraftStorage | null {
  try { return window.sessionStorage; } catch { return null; }
}

export function clearNoteDraft(storage: NoteDraftStorage | null, key: string): boolean {
  try {
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch { return false; }
}

function validDraft(value: unknown): value is NoteDraft {
  if (!value || typeof value !== 'object') return false;
  const draft = value as Partial<NoteDraft>;
  return typeof draft.title === 'string' && draft.title.length <= 1024
    && typeof draft.content === 'string' && draft.content.length <= 2_000_000
    && (draft.baseVersion === null || (Number.isSafeInteger(draft.baseVersion) && Number(draft.baseVersion) > 0));
}

function textBytes(value: string): number { return new TextEncoder().encode(value).length; }

export function readNoteDraft(storage: NoteDraftStorage | null, key: string, now = Date.now()): DraftReadResult {
  const empty = { draft: null, savedAt: null };
  try {
    if (!storage) return { ...empty, status: 'unavailable' };
    const raw = storage.getItem(key);
    if (raw === null) return { ...empty, status: 'empty' };
    const discard = (status: 'expired' | 'invalid'): DraftReadResult => ({
      ...empty, status: clearNoteDraft(storage, key) ? status : 'unavailable',
    });
    if (raw.length > NOTE_DRAFT_MAX_BYTES || textBytes(raw) > NOTE_DRAFT_MAX_BYTES) return discard('invalid');
    let value;
    try { value = JSON.parse(raw); } catch { return discard('invalid'); }
    if (!value || value.schema !== 1 || !Number.isSafeInteger(value.savedAt) || value.savedAt < 0
      || value.savedAt > now + 60_000 || !validDraft(value.draft)) return discard('invalid');
    if (now - value.savedAt >= NOTE_DRAFT_TTL_MS) return discard('expired');
    const { title, content, baseVersion } = value.draft;
    return { draft: { title, content, baseVersion }, savedAt: value.savedAt, status: 'restored' };
  } catch { return { ...empty, status: 'unavailable' }; }
}

export function writeNoteDraft(storage: NoteDraftStorage | null, key: string, draft: NoteDraft, now = Date.now()): DraftWriteStatus {
  if (!validDraft(draft)) return 'too-large';
  // Reconstruct the payload explicitly so additional request/form properties
  // cannot enter browser storage, even when a caller passes a wider object.
  const raw = JSON.stringify({ schema: 1, savedAt: now, draft: {
    title: draft.title, content: draft.content, baseVersion: draft.baseVersion,
  } });
  if (textBytes(raw) > NOTE_DRAFT_MAX_BYTES) return 'too-large';
  try {
    if (!storage) return 'unavailable';
    storage.setItem(key, raw);
    return 'saved';
  } catch { return 'unavailable'; }
}

export function noteSnapshot(value: unknown): NoteDraft {
  if (!value || typeof value !== 'object') throw new Error('无法读取这条随手记');
  const note = value as { source_type?: string; title?: unknown; current_version?: { id?: unknown; raw_content?: unknown } };
  if (note.source_type !== 'note') throw new Error('这条资料不是随手记');
  const version = note.current_version;
  if (!version || !Number.isSafeInteger(version.id) || Number(version.id) <= 0
    || typeof version.raw_content !== 'string' || typeof note.title !== 'string') {
    throw new Error('这条随手记的正文版本无法读取，请重试或先到详情页检查');
  }
  return { title: note.title, content: version.raw_content, baseVersion: Number(version.id) };
}

export function sameNote(left: NoteDraft, right: NoteDraft): boolean {
  return left.baseVersion === right.baseVersion && left.title === right.title && left.content === right.content;
}
