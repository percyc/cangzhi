'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { fetchAuthStatus, type AuthStatus } from '@/lib/api';

const NAV_ITEMS = [
  { href: '/documents', label: '知识库', icon: 'library' },
  { href: '/inbox', label: '收件箱', icon: 'inbox' },
  { href: '/search', label: '搜资料', icon: 'search' },
  { href: '/ask', label: '问知识', icon: 'ask' },
  { href: '/settings', label: '设置', icon: 'settings' },
] as const;

type LoadState =
  | { kind: 'pending' }
  | { kind: 'ready'; status: AuthStatus }
  | { kind: 'error'; message: string };

type Workspace = {
  slug: string;
  name: string;
  description: string | null;
  is_default: boolean;
  status: 'active' | 'archived';
  settings: Record<string, unknown>;
};

type WorkspaceLoadState =
  | { kind: 'idle' }
  | { kind: 'pending' }
  | { kind: 'ready'; list: Workspace[]; current: Workspace | null }
  | { kind: 'fallback' };

export function TopNav() {
  const pathname = usePathname() ?? '/';
  const router = useRouter();
  const [state, setState] = useState<LoadState>({ kind: 'pending' });
  const [workspaceState, setWorkspaceState] = useState<WorkspaceLoadState>({
    kind: 'idle',
  });

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

  const isAuthenticated =
    state.kind === 'ready' && state.status.authenticated;

  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    let cancelled = false;
    Promise.all([
      fetchJson<Workspace[]>('/api/workspaces', '工作空间读取失败'),
      fetchOptional<Workspace>('/api/workspaces/current'),
    ])
      .then(([list, current]) => {
        if (cancelled) return;
        setWorkspaceState({ kind: 'ready', list, current });
      })
      .catch(() => {
        if (cancelled) return;
        setWorkspaceState({ kind: 'fallback' });
      });
    return () => {
      cancelled = true;
    };
  }, [pathname, isAuthenticated]);

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
  const immersiveAsk = pathname.startsWith('/ask');

  return (
    <header className={`${immersiveAsk ? 'hidden sm:block' : ''} sticky top-0 z-40 border-b border-slate-200/80 bg-white/90 shadow-[0_1px_0_rgba(15,23,42,0.02)] backdrop-blur-xl`}>
      <div className="mx-auto flex h-16 max-w-7xl items-center gap-4 px-4 sm:px-6">
        <Link
          href="/documents"
          className="group flex shrink-0 items-center gap-2.5"
          aria-label="藏知 Cangzhi 首页"
          title="藏知 Cangzhi｜藏有所知，问有所据"
        >
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-950 text-base font-semibold text-white shadow-sm transition-transform group-hover:-rotate-3">
            藏
          </span>
          <span className="text-lg font-semibold tracking-tight text-slate-950">藏知</span>
        </Link>

        {isAuthenticated && (
          <WorkspaceSelector state={workspaceState} />
        )}

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
            <Menu
              buttonTitle={status.admin?.username}
              buttonClassName="flex cursor-pointer items-center gap-2 rounded-xl border border-slate-200 bg-white p-1.5 pr-2.5 text-sm text-slate-700 hover:border-slate-300 hover:bg-slate-50"
              panelClassName="w-48 rounded-2xl border border-slate-200 bg-white p-2 shadow-xl shadow-slate-950/10"
              trigger={(open) => (
                <>
                  <span className="grid h-7 w-7 place-items-center rounded-lg bg-slate-100 text-xs font-semibold text-slate-600">
                    {status.admin?.username?.slice(0, 1).toUpperCase() || '我'}
                  </span>
                  <span className="hidden max-w-28 truncate lg:inline">
                    {status.admin?.username}
                  </span>
                  <ChevronIcon open={open} />
                </>
              )}
            >
              {(close) => (
                <>
                  <p className="truncate px-3 py-2 text-xs text-slate-400">
                    已登录为 {status.admin?.username}
                  </p>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      close();
                      void handleLogout();
                    }}
                    className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100"
                  >
                    <LogoutIcon />
                    退出登录
                  </button>
                </>
              )}
            </Menu>
          )}
        </div>
      </div>

      <nav className="grid grid-cols-5 border-t border-slate-100 bg-white/95 px-1 sm:hidden">
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
    <Menu
      buttonClassName="flex cursor-pointer items-center gap-1.5 rounded-xl bg-slate-950 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800"
      panelClassName="w-48 rounded-2xl border border-slate-200 bg-white p-2 text-sm shadow-xl shadow-slate-950/10"
      trigger={() => (
        <>
          <PlusIcon />
          <span className={compact ? 'hidden xs:inline sm:inline' : ''}>添加</span>
        </>
      )}
    >
      {(close) => (
        <>
          <AddLink href="/notes/new" title="记录想法" description="快速写下灵感与笔记" onNavigate={close} />
          <AddLink href="/links/new" title="收藏链接" description="自动提取网页正文" onNavigate={close} />
          <AddLink href="/files/upload" title="上传文件" description="导入文档并自动解析" onNavigate={close} />
        </>
      )}
    </Menu>
  );
}

function AddLink({
  href,
  title,
  description,
  onNavigate,
}: {
  href: string;
  title: string;
  description: string;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={href}
      role="menuitem"
      onClick={onNavigate}
      className="block rounded-xl px-3 py-2.5 hover:bg-slate-100"
    >
      <span className="block font-medium text-slate-800">{title}</span>
      <span className="mt-0.5 block text-[11px] text-slate-400">{description}</span>
    </Link>
  );
}

function Menu({
  trigger,
  children,
  align = 'right',
  buttonClassName = '',
  buttonTitle,
  panelClassName = '',
}: {
  trigger: (open: boolean) => ReactNode;
  children: ReactNode | ((close: () => void) => ReactNode);
  align?: 'left' | 'right';
  buttonClassName?: string;
  buttonTitle?: string;
  panelClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.requestAnimationFrame(() => {
      containerRef.current
        ?.querySelector<HTMLElement>('[role^="menuitem"]')
        ?.focus();
    });
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const close = useCallback(() => setOpen(false), []);

  return (
    <div
      ref={containerRef}
      className="relative shrink-0"
      onKeyDown={(event) => {
        if (!open || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          return;
        }
        const entries = Array.from(
          containerRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [],
        );
        if (entries.length === 0) return;
        event.preventDefault();
        const currentIndex = entries.indexOf(document.activeElement as HTMLElement);
        if (event.key === 'Home') {
          entries[0].focus();
        } else if (event.key === 'End') {
          entries[entries.length - 1].focus();
        } else {
          const delta = event.key === 'ArrowDown' ? 1 : -1;
          const nextIndex =
            currentIndex < 0
              ? delta > 0
                ? 0
                : entries.length - 1
              : (currentIndex + delta + entries.length) % entries.length;
          entries[nextIndex].focus();
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        title={buttonTitle}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
        className={buttonClassName}
      >
        {trigger(open)}
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute z-50 mt-2 ${align === 'right' ? 'right-0' : 'left-0'} ${panelClassName}`}
        >
          {typeof children === 'function' ? children(close) : children}
        </div>
      )}
    </div>
  );
}

function isActive(pathname: string, href: string) {
  return (
    pathname === href ||
    pathname.startsWith(`${href}/`)
  );
}

function NavIcon({ name }: { name: (typeof NAV_ITEMS)[number]['icon'] }) {
  const paths = {
    library: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v15H6.5A2.5 2.5 0 0 0 4 20.5v-15Z" /><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v15h4.5a2.5 2.5 0 0 1 2.5 2.5v-15Z" /></>,
    inbox: <><path d="M4 4h16v12H4z" /><path d="M4 13h4l2 3h4l2-3h4" /></>,
    search: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></>,
    ask: <><path d="M5 5.5h14v10H9l-4 3v-13Z" /><path d="M9 9h6M9 12h4" /></>,
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
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`h-3.5 w-3.5 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden
    >
      <path d="m6 8 4 4 4-4" />
    </svg>
  );
}
function LogoutIcon() {
  return <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden><path d="M8 4H4v12h4M12 6l4 4-4 4M6 10h10" /></svg>;
}

function WorkspaceIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4 text-slate-500"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H8l1.5 1.5h6A1.5 1.5 0 0 1 17 8v5.5A1.5 1.5 0 0 1 15.5 15h-11A1.5 1.5 0 0 1 3 13.5v-7Z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4 text-emerald-600"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m4.5 10.5 3.5 3.5 7.5-8" />
    </svg>
  );
}

function WorkspaceSelector({ state }: { state: WorkspaceLoadState }) {
  if (state.kind === 'idle' || state.kind === 'pending') {
    return null;
  }
  const fallback = state.kind === 'fallback';
  const list = state.kind === 'ready' ? state.list : [];
  const current = state.kind === 'ready' ? state.current : null;
  const active = list.filter((workspace) => workspace.status === 'active');
  const label = current?.name ?? '默认空间';

  const handleSelect = (slug: string) => {
    if (current?.slug === slug) return;
    setWorkspaceCookie(slug);
    if (typeof window !== 'undefined') {
      window.location.reload();
    }
  };

  return (
    <Menu
      align="left"
      buttonTitle={label}
      buttonClassName="flex cursor-pointer items-center gap-1.5 rounded-xl border border-slate-200 bg-white py-1.5 pl-2 pr-2 text-xs text-slate-700 hover:border-slate-300 hover:bg-slate-50 sm:gap-2 sm:py-2 sm:pl-2.5 sm:pr-2.5 sm:text-sm"
      panelClassName="w-72 rounded-2xl border border-slate-200 bg-white p-2 shadow-xl shadow-slate-950/10"
      trigger={(open) => (
        <>
          <WorkspaceIcon />
          <span
            aria-label={fallback ? '当前工作空间：默认空间（暂未加载）' : `当前工作空间：${label}`}
            className="max-w-16 truncate sm:max-w-32"
          >
            {label}
          </span>
          <ChevronIcon open={open} />
        </>
      )}
    >
      {(close) => (
        <>
          <p className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-slate-400">
            切换工作空间
          </p>
          {active.length === 0 ? (
            <p className="px-3 py-3 text-xs text-slate-500">暂无可切换的工作空间</p>
          ) : (
            active.map((workspace) => {
              const isCurrent = current?.slug === workspace.slug;
              return (
                <button
                  key={workspace.slug}
                  type="button"
                  role="menuitemradio"
                  aria-checked={isCurrent}
                  onClick={() => {
                    close();
                    handleSelect(workspace.slug);
                  }}
                  className={`flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left text-sm transition-colors ${
                    isCurrent
                      ? 'bg-slate-50 text-slate-900'
                      : 'text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{workspace.name}</span>
                    {workspace.description ? (
                      <span className="mt-0.5 block truncate text-[11px] text-slate-400">
                        {workspace.description}
                      </span>
                    ) : null}
                  </span>
                  {isCurrent ? <CheckIcon /> : null}
                </button>
              );
            })
          )}
          <div className="mt-1 border-t border-slate-100 px-3 py-2">
            <Link
              href="/settings/workspaces"
              role="menuitem"
              onClick={close}
              className="block text-xs font-medium text-slate-500 hover:text-slate-800"
            >
              管理工作空间 →
            </Link>
          </div>
        </>
      )}
    </Menu>
  );
}

async function fetchJson<T>(url: string, fallbackMessage: string): Promise<T> {
  const response = await fetch(url, {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(fallbackMessage);
  }
  const text = await response.text();
  if (!text) {
    throw new Error('empty response');
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error('invalid json');
  }
}

async function fetchOptional<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url, {
      cache: 'no-store',
      credentials: 'include',
    });
    if (!response.ok) {
      return null;
    }
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      return null;
    }
  } catch {
    return null;
  }
}

function setWorkspaceCookie(slug: string) {
  if (typeof document === 'undefined') return;
  const encoded = encodeURIComponent(slug);
  document.cookie = `cangzhi_workspace=${encoded}; Path=/; Max-Age=31536000; SameSite=Lax`;
}
