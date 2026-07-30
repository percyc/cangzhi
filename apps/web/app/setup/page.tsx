'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

export default function SetupPage() {
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/auth/setup-status', { cache: 'no-store', credentials: 'include' })
      .then((res) => res.json())
      .then((data: { setup_required?: boolean; has_admin?: boolean }) => {
        if (cancelled) return;
        setNeedsSetup(Boolean(data.setup_required));
        if (data.has_admin) {
          router.replace('/login');
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError('无法连接服务器，请稍后重试');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const cleanUsername = username.trim();
    if (!cleanUsername) {
      setError('请输入用户名');
      return;
    }
    if (password.length < 8) {
      setError('密码至少需要 8 个字符');
      return;
    }
    if (password !== confirm) {
      setError('两次输入的密码不一致');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await fetch('/api/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ username: cleanUsername, password }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const detail = (data as { detail?: { message?: string } | string })?.detail;
        if (detail && typeof detail === 'object' && detail.message) {
          setError(detail.message);
        } else if (typeof detail === 'string') {
          setError(detail);
        } else {
          setError('设置失败，请稍后重试');
        }
        return;
      }
      router.replace('/settings');
      router.refresh();
    } catch {
      setError('无法连接服务器，请检查网络');
    } finally {
      setBusy(false);
    }
  };

  if (needsSetup === false) {
    return (
      <main className="mx-auto max-w-md px-6 py-16">
        <p className="text-sm text-slate-500">已经设置过管理员账户，正在跳转到登录…</p>
      </main>
    );
  }

  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-7 flex items-center justify-center gap-3">
          <span className="grid h-11 w-11 place-items-center rounded-2xl bg-slate-950 text-lg font-semibold text-white shadow-lg shadow-slate-950/15">藏</span>
          <span className="text-xl font-semibold tracking-tight text-slate-950">藏知</span>
        </div>
      <form className="space-y-5 rounded-2xl border bg-white p-6 sm:p-8" onSubmit={handleSubmit}>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">只需一分钟</p>
          <h1 className="mt-1 text-2xl font-semibold text-slate-950">创建管理员账户</h1>
          <p className="mt-2 text-sm text-slate-500">这是藏知唯一的本地管理账户，请妥善保存密码。</p>
        </div>
        <div>
          <label htmlFor="username" className="block text-sm text-slate-700">
            用户名
          </label>
          <input
            id="username"
            name="username"
            type="text"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm"
          />
          <p className="mt-1 text-xs text-slate-500">3-32 个字符，可使用字母、数字、下划线、点和短横线。</p>
        </div>
        <div>
          <label htmlFor="password" className="block text-sm text-slate-700">
            密码
          </label>
          <input
            id="password"
            name="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="new-password"
            className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm"
          />
          <p className="mt-1 text-xs text-slate-500">至少 8 个字符，建议使用密码管理器生成。</p>
        </div>
        <div>
          <label htmlFor="confirm" className="block text-sm text-slate-700">
            再次输入密码
          </label>
          <input
            id="confirm"
            name="confirm"
            type="password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            autoComplete="new-password"
            className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm"
          />
        </div>
        {error && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-400"
        >
          {busy ? '正在创建…' : '创建管理员账户'}
        </button>
        </form>
      <p className="mt-5 text-center text-xs text-slate-500">
        已经设置过？<Link href="/login" className="text-slate-700 underline">前往登录</Link>
      </p>
      </div>
    </main>
  );
}
