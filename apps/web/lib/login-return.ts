import { WEB_BASE_PATH } from './paths';

// Return paths are navigation only, never arbitrary URLs or executable schemes.
// Next's router adds basePath, so strip a gateway prefix already in the URL.
export function safeLoginReturn(value: string | null | undefined): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u0020]/.test(value)) return '/documents';
  try {
    const url = new URL(value, 'https://cangzhi.invalid');
    if (url.origin !== 'https://cangzhi.invalid') return '/documents';
    const decoded = decodeURIComponent(url.pathname);
    if (decoded.startsWith('//') || /[\\\u0000-\u0020]/.test(decoded)) return '/documents';
    const pathname = WEB_BASE_PATH && (url.pathname === WEB_BASE_PATH || url.pathname.startsWith(`${WEB_BASE_PATH}/`))
      ? url.pathname.slice(WEB_BASE_PATH.length) || '/' : url.pathname;
    if (['/login', '/setup'].includes(pathname.replace(/\/+$/, '')) || pathname.startsWith('/api/')) return '/documents';
    return `${pathname}${url.search}${url.hash}`;
  } catch { return '/documents'; }
}
