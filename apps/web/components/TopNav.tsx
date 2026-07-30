'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { fetchAuthStatus, type AuthStatus } from '@/lib/api';

const NAV_ITEMS = [
  { href: '/documents', label: '知识库', icon: 'library' },
  { href: '/inbox', label: '收件箱', icon: 'inbox' },
  { href: '/search', label: '搜索', icon: 'search' },
  { href: '/settings', label: '设置', icon: 'settings' },
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
        if (!cancelled) setState({ kind: 'ready', status: data });
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
      if (!response.ok) throw new Error('退出失败');
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

  if (pathname === '/login' || pathname === '/setup') return null;

  const status = state.kind === 'ready' ? state.status : null;
  const error = state.kind === 'error' ? state.message : null;

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200/80 bg-white/90 shadow-[0_1px_0_rgba(15,23,42,0.02)] backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-7xl items-center gap-4 px-4 sm:px-6">
        <Link
          href="/documents"
          className="group flex shrink-0 items-center gap-2.5"
          aria-label="藏知首页"
        >
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-950 text-base font-semibold text-white shadow-sm transition-transform group-hover:-rotate-3">
            藏
          </span>
          <span className="text-lg font-semibold tracking-tight text-slate-950">藏知</span>
        </Link>

        <nav className="ml-2 hidden flex-1 items-center gap-1 sm:flex">
          {NAV_ITEMS.map((item) => (
            <NavItem
              key={item.href}
              item={item}
              active={isActive(pathname, item.href)}
            />
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <AddMenu compact />
          {state.kind === 'pending' && (
            <span className="hidden text-xs text-slate-400 lg:inline">正在连接…</span>
          )}
          {state.kind === 'ready' && status && !status.authenticated && !status.setup_required && (
            <Link
              href="/login"
              className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:border-slate-300 hover:bg-slate-50"
            >
              登录
            </Link>
          )}
          {status?.authenticated && (
            <details className="group relative">
              <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl border border-slate-200 bg-white p-1.5 pr-2.5 text-sm text-slate-700 hover:border-slate-300 hover:bg-slate-50">
                <span className="grid h-7 w-7 place-items-center rounded-lg bg-slate-100 text-xs font-semibold text-slate-600">
                  {status.admin?.username?.slice(0, 1).toUpperCase() || '我'}
                </span>
                <span className="hidden max-w-28 truncate lg:inline">
                  {status.admin?.username}
                </span>
                <ChevronIcon />
              </summary>
              <div className="absolute right-0 z-50 mt-2 w-48 rounded-2xl border border-slate-200 bg-white p-2 shadow-xl shadow-slate-950/10">
                <p className="truncate px-3 py-2 text-xs text-slate-400">
                  已登录为 {status.admin?.username}
                </p>
                <button
                  type="button"
                  onClick={handleLogout}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100"
                >
                  <LogoutIcon />
                  退出登录
                </button>
              </div>
            </details>
          )}
        </div>
      </div>

      <nav className="grid grid-cols-4 border-t border-slate-100 bg-white/95 px-2 sm:hidden">
        {NAV_ITEMS.map((item) => (
          <NavItem
            key={item.href}
            item={item}
            active={isActive(pathname, item.href)}
            mobile
          />
        ))}
      </nav>

      {error && (
        <div className="border-t border-red-200 bg-red-50 text-xs text-red-700">
          <div className="mx-auto max-w-7xl px-4 py-2 sm:px-6">{error}</div>
        </div>
      )}
    </header>
  );
}

function NavItem({
  item,
  active,
  mobile = false,
}: {
  item: (typeof NAV_ITEMS)[number];
  active: boolean;
  mobile?: boolean;
}) {
  if (mobile) {
    return (
      <Link
        href={item.href}
        className={`relative flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px] font-medium ${
          active ? 'text-slate-950' : 'text-slate-500'
        }`}
      >
        <NavIcon name={item.icon} />
        {item.label}
        {active && <span className="absolute inset-x-4 bottom-0 h-0.5 rounded-full bg-slate-950" />}
      </Link>
    );
  }
  return (
    <Link
      href={item.href}
      className={`rounded-xl px-3.5 py-2 text-sm font-medium transition-colors ${
        active
          ? 'bg-slate-100 text-slate-950'
          : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
      }`}
    >
      {item.label}
    </Link>
  );
}

function AddMenu({ compact = false }: { compact?: boolean }) {
  return (
    <details className="group relative">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 rounded-xl bg-slate-950 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800">
        <PlusIcon />
        <span className={compact ? 'hidden xs:inline sm:inline' : ''}>添加</span>
      </summary>
      <div className="absolute right-0 z-50 mt-2 w-48 rounded-2xl border border-slate-200 bg-white p-2 text-sm shadow-xl shadow-slate-950/10">
        <AddLink href="/notes/new" title="记录想法" description="快速写下灵感与笔记" />
        <AddLink href="/links/new" title="收藏链接" description="自动提取网页正文" />
        <AddLink href="/files/upload" title="上传文件" description="导入文档并自动解析" />
      </div>
    </details>
  );
}

function AddLink({
  href,
  title,
  description,
}: {
  href: string;
  title: string;
  description: string;
}) {
  return (
    <Link href={href} className="block rounded-xl px-3 py-2.5 hover:bg-slate-100">
      <span className="block font-medium text-slate-800">{title}</span>
      <span className="mt-0.5 block text-[11px] text-slate-400">{description}</span>
    </Link>
  );
}

function isActive(pathname: string, href: string) {
  return (
    pathname === href ||
    pathname.startsWith(`${href}/`) ||
    (href === '/search' && pathname.startsWith('/ask'))
  );
}

function NavIcon({ name }: { name: (typeof NAV_ITEMS)[number]['icon'] }) {
  const paths = {
    library: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v15H6.5A2.5 2.5 0 0 0 4 20.5v-15Z" /><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v15h4.5a2.5 2.5 0 0 1 2.5 2.5v-15Z" /></>,
    inbox: <><path d="M4 4h16v12H4z" /><path d="M4 13h4l2 3h4l2-3h4" /></>,
    search: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.1A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3v-4h.1A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.1A1.7 1.7 0 0 0 15.4 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.15.37.37.7.6 1 .3.3.7.4 1.1.4h.1v4h-.1a1.7 1.7 0 0 0-1.7.6Z" /></>,
  };
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {paths[name]}
    </svg>
  );
}

function PlusIcon() {
  return <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M10 4v12M4 10h12" /></svg>;
}
function ChevronIcon() {
  return <svg viewBox="0 0 20 20" className="h-3.5 w-3.5 text-slate-400 transition-transform group-open:rotate-180" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="m6 8 4 4 4-4" /></svg>;
}
function LogoutIcon() {
  return <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden><path d="M8 4H4v12h4M12 6l4 4-4 4M6 10h10" /></svg>;
}
