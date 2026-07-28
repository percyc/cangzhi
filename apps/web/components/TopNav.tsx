'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { fetchAuthStatus, type AuthStatus } from '@/lib/api';

const NAV_ITEMS = [
  { href: '/documents', label: '资料库' },
  { href: '/search', label: '搜索' },
  { href: '/ask', label: '问知识库' },
  { href: '/settings', label: '设置' },
] as const;

type LoadState =
  | { kind: 'pending' }
  | { kind: 'ready'; status: AuthStatus }
  | { kind: 'error'; message: string };

export function TopNav() {
  const pathname = usePathname() ?? '/';
  const router = useRouter();
  const [state, setState] = useState<LoadState>({ kind: 'pending' });

  useEffect(() => {
    let cancelled = false;
    fetchAuthStatus()
      .then((data) => {
        if (cancelled) return;
        setState({ kind: 'ready', status: data });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : '登录状态读取失败',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  const handleLogout = async () => {
    try {
      const response = await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
      });
      if (!response.ok) {
        throw new Error('退出失败');
      }
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof Error ? err.message : '退出失败',
      });
      return;
    }
    router.replace('/login');
    router.refresh();
  };

  if (pathname === '/login' || pathname === '/setup') {
    return null;
  }

  const status = state.kind === 'ready' ? state.status : null;
  const error = state.kind === 'error' ? state.message : null;

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-3">
        <Link href="/documents" className="text-lg font-semibold text-slate-900">
          藏知
        </Link>
        <nav className="flex flex-1 flex-wrap items-center gap-2 text-sm">
          {NAV_ITEMS.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            const style = active
              ? 'rounded-full bg-slate-900 px-3 py-1 text-white'
              : 'rounded-full px-3 py-1 text-slate-700 hover:bg-slate-100';
            return (
              <Link key={item.href} href={item.href} className={style}>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          {state.kind === 'pending' && <span>登录状态加载中…</span>}
          {state.kind === 'ready' && status?.authenticated && status.admin && (
            <span>已登录：{status.admin.username}</span>
          )}
          {state.kind === 'ready' && status && !status.authenticated && !status.setup_required && (
            <Link href="/login" className="text-slate-700 hover:underline">
              登录
            </Link>
          )}
          {status?.authenticated && (
            <button
              type="button"
              onClick={handleLogout}
              className="rounded border border-slate-300 px-2 py-1 text-slate-700 hover:bg-slate-50"
            >
              退出
            </button>
          )}
        </div>
      </div>
      {error && (
        <div className="border-t border-red-200 bg-red-50 text-xs text-red-700">
          <div className="mx-auto max-w-6xl px-4 py-2">{error}</div>
        </div>
      )}
    </header>
  );
}
