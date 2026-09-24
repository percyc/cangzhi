'use client';

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';

import { SettingsSectionNav } from '@/components/SettingsSectionNav';

type AdminProfile = {
  id: number;
  username: string;
  created_at: string | null;
  last_login_at: string | null;
};

type LoginSession = {
  id: number;
  current: boolean;
  user_agent: string | null;
  ip_address: string | null;
  last_seen_at: string | null;
  expires_at: string;
};

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', credentials: 'include', ...init });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body?.detail;
    throw new Error(typeof detail?.message === 'string' ? detail.message : '操作失败，请稍后重试');
  }
  return response.json() as Promise<T>;
}

function displayTime(value: string | null): string {
  if (!value) return '暂无记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '暂无记录' : date.toLocaleString('zh-CN');
}

export default function AccountSettingsPage() {
  const router = useRouter();
  const [profile, setProfile] = useState<AdminProfile | null>(null);
  const [sessions, setSessions] = useState<LoginSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [renamePassword, setRenamePassword] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const load = useCallback(async () => {
    const [account, devices] = await Promise.all([
      apiJson<{ admin: AdminProfile }>('/api/auth/account'),
      apiJson<{ items: LoginSession[] }>('/api/auth/sessions'),
    ]);
    setProfile(account.admin);
    setUsername(account.admin.username);
    setSessions(devices.items);
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiJson<{ admin: AdminProfile }>('/api/auth/account'),
      apiJson<{ items: LoginSession[] }>('/api/auth/sessions'),
    ])
      .then(([account, devices]) => {
        if (cancelled) return;
        setProfile(account.admin);
        setUsername(account.admin.username);
        setSessions(devices.items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '账户读取失败');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  async function rename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const result = await apiJson<{ admin: AdminProfile }>('/api/auth/account', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), current_password: renamePassword }),
      });
      setProfile(result.admin);
      setUsername(result.admin.username);
      setRenamePassword('');
      setNotice('用户名已更新，下次登录请使用新用户名。');
      window.dispatchEvent(new Event('cangzhi:auth-updated'));
      router.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '修改用户名失败');
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setNotice(null);
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致');
      return;
    }
    setBusy(true);
    try {
      const result = await apiJson<{ other_sessions_revoked: number }>('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      await load();
      setNotice(`密码已更新；其他 ${result.other_sessions_revoked} 个登录会话已退出。当前设备可继续使用。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '修改密码失败');
    } finally {
      setBusy(false);
    }
  }

  async function revokeOthers() {
    if (busy || !window.confirm('要让其他设备上的藏知会话退出吗？当前设备不会退出。')) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await apiJson<{ revoked: number }>('/api/auth/sessions/revoke-others', { method: 'POST' });
      await load();
      setNotice(`已退出其他 ${result.revoked} 个会话。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '退出其他设备失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">系统设置</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">账户与安全</h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">管理唯一管理员账户，以及哪些设备仍保持登录。</p>
      <SettingsSectionNav active="account" />

      {loading && <p className="mt-6 text-sm text-slate-500">正在读取账户…</p>}
      {error && <p role="alert" className="mt-6 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="mt-6 rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">{notice}</p>}

      {profile && (
        <div className="mt-6 space-y-4">
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-lg font-semibold text-slate-950">管理员资料</h2>
            <div className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
              <div><p className="text-slate-500">用户名</p><p className="mt-1 font-medium text-slate-900">{profile.username}</p></div>
              <div><p className="text-slate-500">创建时间</p><p className="mt-1 font-medium text-slate-900">{displayTime(profile.created_at)}</p></div>
              <div><p className="text-slate-500">最近登录</p><p className="mt-1 font-medium text-slate-900">{displayTime(profile.last_login_at)}</p></div>
            </div>
            <p className="mt-4 text-xs text-slate-500">藏知当前为单管理员模式。工作空间用于隔离知识，不会创建独立登录用户。</p>
          </section>

          <div className="grid gap-4 lg:grid-cols-2">
            <form onSubmit={rename} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="text-lg font-semibold text-slate-950">修改用户名</h2>
              <p className="mt-1 text-sm text-slate-500">修改后下次登录使用新名称；当前会话继续有效。</p>
              <label className="mt-5 block text-sm font-medium text-slate-700" htmlFor="account-username">新用户名</label>
              <input id="account-username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} minLength={3} maxLength={32} required pattern="[A-Za-z0-9_.-]{3,32}" className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm" />
              <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="rename-password">当前密码</label>
              <input id="rename-password" type="password" autoComplete="current-password" value={renamePassword} onChange={(event) => setRenamePassword(event.target.value)} minLength={8} required className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm" />
              <button type="submit" disabled={busy || username.trim().toLowerCase() === profile.username} className="mt-5 rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50">保存用户名</button>
            </form>

            <form onSubmit={changePassword} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="text-lg font-semibold text-slate-950">修改密码</h2>
              <p className="mt-1 text-sm text-slate-500">修改后其他设备会退出，当前设备保留登录。个人访问令牌不会自动撤销。</p>
              <label className="mt-5 block text-sm font-medium text-slate-700" htmlFor="current-password">当前密码</label>
              <input id="current-password" type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} minLength={8} required className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm" />
              <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="new-password">新密码（至少 8 位）</label>
              <input id="new-password" type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} minLength={8} required className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm" />
              <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="confirm-password">再次输入新密码</label>
              <input id="confirm-password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} minLength={8} required className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm" />
              <button type="submit" disabled={busy} className="mt-5 rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50">更新密码</button>
            </form>
          </div>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div><h2 className="text-lg font-semibold text-slate-950">登录设备</h2><p className="mt-1 text-sm text-slate-500">仅显示仍有效的浏览器会话，最多保留 16 个。</p></div>
              <button type="button" disabled={busy || sessions.filter((item) => !item.current).length === 0} onClick={() => void revokeOthers()} className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-50">退出其他设备</button>
            </div>
            <div className="mt-4 divide-y divide-slate-100">
              {sessions.map((item) => (
                <div key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                  <div className="min-w-0"><p className="break-all font-medium text-slate-800">{item.current && <span className="mr-2 rounded-md bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800">当前设备</span>}{item.user_agent || '未知浏览器'}</p><p className="mt-1 text-xs text-slate-500">{item.ip_address || '未知地址'} · 最近活动 {displayTime(item.last_seen_at)} · 有效至 {displayTime(item.expires_at)}</p></div>
                </div>
              ))}
              {sessions.length === 0 && <p className="py-4 text-sm text-slate-500">没有有效会话。</p>}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
