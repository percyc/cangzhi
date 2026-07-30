'use client';

import Link from 'next/link';
import { FormEvent, useCallback, useEffect, useState } from 'react';

type Counts = { total: number; pending: number; failed: number; synced: number };
type Source = {
  id: number;
  name: string;
  base_url: string;
  username: string;
  root_path: string;
  recursive: boolean;
  trusted_private_network: boolean;
  sync_status: string;
  last_error: string | null;
  last_scan_at: string | null;
  last_sync_at: string | null;
  entry_counts: Counts;
};
type Entry = {
  id: number;
  remote_path: string;
  file_size: number | null;
  state: string;
  document_id: number | null;
  last_error: string | null;
};

const EMPTY = {
  name: '',
  base_url: '',
  username: '',
  password: '',
  root_path: '/',
  trusted_private_network: false,
};

export default function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [entries, setEntries] = useState<Record<number, Entry[]>>({});
  const [form, setForm] = useState(EMPTY);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    const response = await fetch('/api/webdav', { cache: 'no-store' });
    if (!response.ok) throw new Error(await readError(response, '知识源读取失败'));
    setSources((await response.json()) as Source[]);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load().catch((caught) =>
        setError(caught instanceof Error ? caught.message : '知识源读取失败'),
      );
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    setCreating(true);
    setError('');
    try {
      const response = await fetch('/api/webdav', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      if (!response.ok) throw new Error(await readError(response, '添加知识源失败'));
      setForm(EMPTY);
      setMessage('知识源已添加，建议先测试连接，再扫描并同步。');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '添加知识源失败');
    } finally {
      setCreating(false);
    }
  };

  const action = async (source: Source, kind: 'test' | 'scan' | 'sync') => {
    setBusy(`${source.id}:${kind}`);
    setError('');
    setMessage('');
    try {
      const response = await fetch(`/api/webdav/${source.id}/${kind}`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error(await readError(response, '操作失败'));
      const result = (await response.json()) as Record<string, unknown>;
      if (kind === 'test') setMessage(`${source.name}：连接正常`);
      if (kind === 'scan')
        setMessage(
          `${source.name}：扫描到 ${String(result.eligible_files ?? 0)} 个可导入文件`,
        );
      if (kind === 'sync')
        setMessage(
          `${source.name}：新增 ${String(result.imported ?? 0)}，更新 ${String(result.updated ?? 0)}，失败 ${String(result.failed ?? 0)}`,
        );
      await load();
      if (kind !== 'test') await showEntries(source.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '操作失败');
    } finally {
      setBusy('');
    }
  };

  const showEntries = async (sourceId: number) => {
    if (entries[sourceId]) {
      setEntries((current) => {
        const next = { ...current };
        delete next[sourceId];
        return next;
      });
      return;
    }
    const response = await fetch(`/api/webdav/${sourceId}/entries`, {
      cache: 'no-store',
    });
    if (!response.ok) throw new Error(await readError(response, '文件清单读取失败'));
    const items = (await response.json()) as Entry[];
    setEntries((current) => ({
      ...current,
      [sourceId]: items,
    }));
  };

  const remove = async (source: Source) => {
    if (
      !window.confirm(
        `确定删除连接器“${source.name}”吗？已解析入库的资料会保留，远端文件不会被删除。`,
      )
    ) return;
    setBusy(`${source.id}:delete`);
    setError('');
    try {
      const response = await fetch(`/api/webdav/${source.id}`, {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error(await readError(response, '删除连接器失败'));
      setMessage(`连接器“${source.name}”已删除，已入库资料仍然保留。`);
      setEntries((current) => {
        const next = { ...current };
        delete next[source.id];
        return next;
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除连接器失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <main className="container mx-auto max-w-6xl p-4">
      <Link href="/settings" className="text-sm text-blue-700 hover:underline">← 返回设置</Link>
      <h1 className="mt-4 text-2xl font-bold">知识源</h1>
      <p className="mt-1 text-sm text-slate-500">
        连接外部文件夹，藏知只读扫描远端文件，下载后沿用正文解析、智能切片和向量流程。
      </p>

      <form onSubmit={create} className="mt-6 rounded-xl border bg-white p-5">
        <h2 className="font-semibold">添加 WebDAV 文件夹</h2>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <Field label="名称" value={form.name} onChange={(name) => setForm({ ...form, name })} />
          <Field label="WebDAV 地址" value={form.base_url} onChange={(base_url) => setForm({ ...form, base_url })} placeholder="https://example.com/dav/" />
          <Field label="用户名" value={form.username} onChange={(username) => setForm({ ...form, username })} />
          <Field label="密码" type="password" value={form.password} onChange={(password) => setForm({ ...form, password })} />
          <Field label="同步目录" value={form.root_path} onChange={(root_path) => setForm({ ...form, root_path })} />
          <label className="flex items-end gap-2 pb-2 text-sm">
            <input type="checkbox" checked={form.trusted_private_network} onChange={(event) => setForm({ ...form, trusted_private_network: event.target.checked })} />
            允许连接可信内网地址
          </label>
        </div>
        <button disabled={creating} className="mt-4 rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50">
          {creating ? '正在添加…' : '添加知识源'}
        </button>
      </form>

      {message && <p className="mt-4 rounded bg-emerald-50 p-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="mt-4 rounded bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      <div className="mt-6 space-y-4">
        {sources.map((source) => (
          <section key={source.id} className="rounded-xl border bg-white p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold">{source.name}</h2>
                <p className="mt-1 break-all text-xs text-slate-500">{source.base_url}{source.root_path}</p>
                <p className="mt-2 text-sm text-slate-600">
                  文件 {source.entry_counts.total} · 已同步 {source.entry_counts.synced} · 待同步 {source.entry_counts.pending} · 失败 {source.entry_counts.failed}
                </p>
              </div>
              <span className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600">
                {statusLabel(source.sync_status)}
              </span>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              {(['test', 'scan', 'sync'] as const).map((kind) => (
                <button key={kind} type="button" disabled={Boolean(busy)} onClick={() => void action(source, kind)} className="rounded border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40">
                  {busy === `${source.id}:${kind}` ? '执行中…' : { test: '测试连接', scan: '扫描文件', sync: '同步入库' }[kind]}
                </button>
              ))}
              <button type="button" onClick={() => void showEntries(source.id).catch((caught) => setError(caught instanceof Error ? caught.message : '读取失败'))} className="rounded px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100">
                {entries[source.id] ? '收起清单' : '查看文件'}
              </button>
              <button type="button" disabled={Boolean(busy)} onClick={() => void remove(source)} className="rounded px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40">
                {busy === `${source.id}:delete` ? '删除中…' : '删除连接器'}
              </button>
            </div>
            {source.last_error && <p className="mt-3 text-sm text-red-700">{source.last_error}</p>}
            {entries[source.id] && <EntryTable entries={entries[source.id]} />}
          </section>
        ))}
        {!sources.length && <p className="rounded-xl border border-dashed p-8 text-center text-sm text-slate-500">还没有知识源。</p>}
      </div>
    </main>
  );
}

function Field({ label, value, onChange, type = 'text', placeholder = '' }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return <label className="text-sm text-slate-700">{label}<input required value={value} type={type} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-3 py-2" /></label>;
}

function EntryTable({ entries }: { entries: Entry[] }) {
  return <div className="mt-4 overflow-x-auto border-t pt-3"><table className="min-w-full text-left text-xs"><thead className="text-slate-500"><tr><th className="py-2">远端文件</th><th>状态</th><th>大小</th><th>资料</th></tr></thead><tbody>{entries.map((entry) => <tr key={entry.id} className="border-t"><td className="max-w-xl break-all py-2 pr-4">{entry.remote_path}</td><td>{entryState(entry.state)}</td><td>{formatSize(entry.file_size)}</td><td>{entry.document_id ? <Link className="text-blue-700 hover:underline" href={`/documents/${entry.document_id}`}>查看</Link> : '—'}</td></tr>)}</tbody></table></div>;
}

function statusLabel(value: string) {
  return ({ idle: '空闲', scanning: '扫描中', syncing: '同步中', failed: '有失败' } as Record<string, string>)[value] ?? value;
}
function entryState(value: string) {
  return ({ discovered: '待同步', changed: '有更新', synced: '已同步', failed: '失败', missing: '远端已删除' } as Record<string, string>)[value] ?? value;
}
function formatSize(value: number | null) {
  if (value == null) return '—';
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
async function readError(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || fallback;
  } catch {
    return fallback;
  }
}
