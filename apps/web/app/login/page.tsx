'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';

function LoginInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = searchParams?.get('next') ?? '/documents';
  const setupRequired = searchParams?.get('setup') === 'required';

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(
    setupRequired ? '系统还没有设置管理员账户，请先完成首次设置。' : null,
  );
  const [busy, setBusy] = useState(false);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!username.trim() || !password) {
      setError('请输入用户名和密码');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          username: username.trim(),
          password,
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const detail = (data as { detail?: { message?: string } | string })?.detail;
        if (detail && typeof detail === 'object' && detail.message) {
          setError(detail.message);
        } else if (typeof detail === 'string') {
          setError(detail);
        } else {
          setError('登录失败，请稍后重试');
        }
        return;
      }
      router.replace(next);
      router.refresh();
    } catch {
      setError('无法连接服务器，请检查网络');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto max-w-md px-6 py-16">
      <h1 className="text-2xl font-semibold text-slate-900">登录藏知</h1>
      <p className="mt-2 text-sm text-slate-500">
        请输入首次设置时创建的管理员账户。当前版本暂不提供自助找回密码，请妥善保存。
      </p>
      <form className="mt-8 space-y-4" onSubmit={handleSubmit}>
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
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
          />
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
            autoComplete="current-password"
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
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
          className="w-full rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:bg-slate-400"
        >
          {busy ? '正在登录…' : '登录'}
        </button>
      </form>
      <p className="mt-6 text-xs text-slate-500">
        还没有设置过账户？<Link href="/setup" className="text-slate-700 underline">前往首次设置</Link>
      </p>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto max-w-md px-6 py-16">
          <p className="text-sm text-slate-500">正在加载登录页…</p>
        </main>
      }
    >
      <LoginInner />
    </Suspense>
  );
}
