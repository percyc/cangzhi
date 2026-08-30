export function normalizeBasePath(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  if (!trimmed || trimmed === '/') return '';
  const stripped = trimmed.replace(/^\/+|\/+$/g, '');
  return stripped ? `/${stripped}` : '';
}

export const WEB_BASE_PATH = normalizeBasePath(
  process.env.NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH,
);

export function withBasePath(path: string): string {
  if (!path) return WEB_BASE_PATH || '/';
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith('#')) return path;
  const normalized = path.startsWith('/') ? path : `/${path}`;
  if (!WEB_BASE_PATH) return normalized;
  if (normalized === WEB_BASE_PATH || normalized.startsWith(`${WEB_BASE_PATH}/`)) {
    return normalized;
  }
  return `${WEB_BASE_PATH}${normalized}`;
}

export function withApiBasePath(path: string): string {
  return withBasePath(path);
}
