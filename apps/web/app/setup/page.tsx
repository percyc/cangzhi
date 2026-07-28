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
    <main className="mx-auto max-w-md px-6 py-16">
      <h1 className="text-2xl font-semibold text-slate-900">首次设置</h1>
      <p className="mt-2 text-sm text-slate-500">
        创建藏知唯一的管理员账户。当前版本暂不提供自助找回密码，请妥善保存。
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
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
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
          {busy ? '正在创建…' : '创建管理员账户'}
        </button>
      </form>
      <p className="mt-6 text-xs text-slate-500">
        已经设置过？<Link href="/login" className="text-slate-700 underline">前往登录</Link>
      </p>
    </main>
  );
}
