// UI state only: retrieval and authorization remain owned by the API.
export function askPreferencesKey(cookie: string, documentIds: readonly number[] = []): string {
  const raw = cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith('cangzhi_workspace='))?.slice('cangzhi_workspace='.length);
  let workspace = 'default';
  try { workspace = raw ? decodeURIComponent(raw) : workspace; } catch { /* Invalid cookies use the server default. */ }
  const documents = [...new Set(documentIds)].sort((a, b) => a - b);
  return `cangzhi:ask-preferences:v3:${encodeURIComponent(workspace)}${documents.length ? `:documents:${documents.join(',')}` : ''}`;
}

export type AskPreferences = {
  scopeSlug: string;
  categoryIds: number[];
  tagIds: number[];
  sourceTypes: string[];
  connectorIds: number[];
  question: string;
  mode: 'quick' | 'deep';
};

export function validateAskPreferences(raw: unknown, catalog: {
  scopes: Array<{ slug: string }>;
  categories: Array<{ id: number }>;
  tags: Array<{ id: number }>;
  source_types: Array<{ value: string }>;
  connectors: Array<{ id: number }>;
}): { preferences: AskPreferences; filtersRemoved: boolean } {
  const value = raw && typeof raw === 'object' ? raw as Partial<AskPreferences> : {};
  let filtersRemoved = false;
  const valid = <T extends string | number>(items: unknown, allowed: T[]): T[] => {
    if (!Array.isArray(items)) return [];
    const result = [...new Set(items.filter((item): item is T => allowed.includes(item)))];
    if (items.some((item) => !allowed.includes(item))) filtersRemoved = true;
    return result;
  };
  const scopeSlug = typeof value.scopeSlug === 'string' && catalog.scopes.some((scope) => scope.slug === value.scopeSlug) ? value.scopeSlug : 'all';
  if (value.scopeSlug && value.scopeSlug !== scopeSlug) filtersRemoved = true;
  return {
    preferences: {
      scopeSlug,
      categoryIds: valid(value.categoryIds, catalog.categories.map((item) => item.id)),
      tagIds: valid(value.tagIds, catalog.tags.map((item) => item.id)),
      sourceTypes: valid(value.sourceTypes, catalog.source_types.map((item) => item.value)),
      connectorIds: valid(value.connectorIds, catalog.connectors.map((item) => item.id)),
      question: typeof value.question === 'string' ? value.question.slice(0, 500) : '',
      mode: value.mode === 'deep' ? 'deep' : 'quick',
    },
    filtersRemoved,
  };
}

export const SEARCH_PAGE_SIZE = 20;
export type SearchLocation = { query: string; categorySlugs: string[]; tagSlugs: string[]; sourceTypes: string[]; page: number };
export function readSearchLocation(params: URLSearchParams): SearchLocation {
  const rawPage = Number(params.get('page') || '1');
  const values = (key: string) => [...new Set(params.getAll(key).filter(Boolean))];
  return {
    query: params.get('q') ?? '',
    categorySlugs: values('category'), tagSlugs: values('tag'), sourceTypes: values('source'),
    page: Number.isInteger(rawPage) && rawPage > 0 ? Math.min(rawPage, 501) : 1,
  };
}
export function searchLocationHref(location: SearchLocation): string {
  const params = new URLSearchParams();
  if (location.query.trim()) params.set('q', location.query.trim());
  for (const value of location.categorySlugs) params.append('category', value);
  for (const value of location.tagSlugs) params.append('tag', value);
  for (const value of location.sourceTypes) params.append('source', value);
  if (location.page > 1) params.set('page', String(location.page));
  return params.size ? `/search?${params}` : '/search';
}
export function searchEvidenceHref(documentId: number, chunk: { id: number; page: number | null }): string {
  const params = new URLSearchParams({ chunk_id: String(chunk.id) });
  if (chunk.page !== null && chunk.page > 0) params.set('page', String(chunk.page));
  return `/documents/${documentId}?${params}`;
}
