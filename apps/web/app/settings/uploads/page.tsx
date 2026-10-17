'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { SettingsSectionNav } from '@/components/SettingsSectionNav';
import { withApiBasePath } from '@/lib/paths';
import { useUnsavedChanges } from '@/lib/navigation-guard';

export default function UploadSettingsPage() {
  const [saved, setSaved] = useState<number | null>(null);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  useUnsavedChanges(saved !== null && value !== String(saved));

  async function load() {
    try {
      const response = await fetch(withApiBasePath('/api/system/upload-settings'), { cache: 'no-store' });
      if (!response.ok) throw new Error('读取上传设置失败，请检查登录状态或重试。');
      const data = await response.json();
      setError('');
      setSaved(data.max_upload_size_mb); setValue(String(data.max_upload_size_mb));
    } catch (err) { setError(err instanceof Error ? err.message : '读取失败'); }
  }
  useEffect(() => {
    let cancelled = false;
    fetch(withApiBasePath('/api/system/upload-settings'), { cache: 'no-store' })
      .then(async response => {
        if (!response.ok) throw new Error('读取上传设置失败，请检查登录状态或重试。');
        const data = await response.json();
        if (!cancelled) {
          setSaved(data.max_upload_size_mb); setValue(String(data.max_upload_size_mb));
        }
      })
      .catch(err => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, []);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const limit = Number(value);
    if (!Number.isInteger(limit) || limit < 1 || limit > 2147483647) {
      setError('请输入有效的正整数（单位 MiB）。'); return;
    }
    setBusy(true); setError(''); setMessage('');
    try {
      const response = await fetch(withApiBasePath('/api/system/upload-settings'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_upload_size_mb: limit }),
      });
      if (!response.ok) throw new Error('保存失败，设置尚未更改，请检查登录状态或重试。');
      const data = await response.json();
      setSaved(data.max_upload_size_mb); setValue(String(data.max_upload_size_mb));
      setMessage('已保存，对新上传请求立即生效，无需重启。');
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败'); }
    finally { setBusy(false); }
  }

  return <main className="settings-shell mx-auto max-w-5xl px-5 py-9">
    <p className="text-xs font-semibold tracking-widest text-slate-400">系统设置</p>
    <h1 className="mt-2 text-3xl font-semibold text-slate-950">文件上传</h1>
    <p className="mt-2 text-sm text-slate-500">所有工作空间共用，网页上传与外部 API 使用相同限额。</p>
    <SettingsSectionNav active="uploads" />
    <form onSubmit={save} className="mt-6 space-y-5 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
      <div>
        <label htmlFor="upload-limit" className="block text-sm font-medium text-slate-800">单个文件大小上限（MiB）</label>
        <input id="upload-limit" type="number" min="1" max="2147483647" step="1" required
          disabled={saved === null || busy} value={value} onChange={event => { setValue(event.target.value); setMessage(''); }}
          aria-describedby="upload-limit-help" className="mt-2 w-full max-w-xs rounded-xl border px-3 py-2.5" />
        <p id="upload-limit-help" className="mt-2 text-sm text-slate-500">默认 50 MiB。1 MiB = 1,048,576 字节。只限制新上传，不影响已保存的文件。</p>
      </div>
      <div className="rounded-xl bg-amber-50 p-4 text-sm leading-6 text-amber-900">
        大文件会增加存储、临时磁盘和解析耗时。提高上传上限不会自动提高 OCR 页数、表格单元格等处理限制。
        <p className="mt-2">如果另外使用 Nginx 等代理，请检查请求体大小限制（Nginx 的 client_max_body_size），并预留表单封装余量。外部代理的限制不由藏知修改。</p>
      </div>
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
      {message && <p role="status" className="text-sm text-emerald-700">{message}</p>}
      <div className="flex flex-wrap gap-3">
        <button disabled={saved === null || busy} className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm text-white disabled:opacity-50">{busy ? '保存中…' : '保存设置'}</button>
        {saved === null && <button type="button" onClick={() => void load()} className="rounded-xl border px-4 py-2">重新读取</button>}
        <button type="button" disabled={saved === null || busy} onClick={() => { setValue('50'); setMessage(''); }} className="rounded-xl border px-4 py-2 text-sm">填入默认值 50</button>
        <Link href="/files/upload" className="px-3 py-2 text-sm text-blue-700">去上传文件</Link>
      </div>
    </form>
  </main>;
}
