'use client';

import Link from 'next/link';
import { FormEvent, useCallback, useEffect, useState } from 'react';

import { SettingsSectionNav } from '@/components/SettingsSectionNav';

type Counts = { total: number; pending: number; failed: number; synced: number };
type Source = {
  id: number;
  name: string;
  base_url: string;
  username: string;
  has_password: boolean;
  root_path: string;
  recursive: boolean;
  trusted_private_network: boolean;
  include_extensions: string[];
  ignore_patterns: string[];
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

type SourceForm = {
  name: string;
  base_url: string;
  username: string;
  password: string;
  root_path: string;
  recursive: boolean;
  trusted_private_network: boolean;
  include_extensions: string;
  ignore_patterns: string;
  clear_password: boolean;
};

const DEFAULT_EXTENSIONS = '.pdf, .doc, .docx, .md, .markdown, .txt';
const DEFAULT_IGNORE_PATTERNS = '.*\n~$*\n*.tmp\n@eaDir';
const EMPTY: SourceForm = {
  name: '',
  base_url: '',
  username: '',
  password: '',
  root_path: '/',
  recursive: true,
  trusted_private_network: false,
  include_extensions: DEFAULT_EXTENSIONS,
  ignore_patterns: DEFAULT_IGNORE_PATTERNS,
  clear_password: false,
};

export default function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [entries, setEntries] = useState<Record<number, Entry[]>>({});
  const [form, setForm] = useState(EMPTY);
  const [editingSourceId, setEditingSourceId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<SourceForm>(EMPTY);
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
        body: JSON.stringify(sourcePayload(form)),
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

  const startEditing = (source: Source) => {
    setEditingSourceId(source.id);
    setEditForm({
      name: source.name,
      base_url: source.base_url,
      username: source.username,
      password: '',
      root_path: source.root_path,
      recursive: source.recursive,
      trusted_private_network: source.trusted_private_network,
      include_extensions: source.include_extensions.join(', '),
      ignore_patterns: source.ignore_patterns.join('\n'),
      clear_password: false,
    });
    setMessage('');
    setError('');
  };

  const save = async (event: FormEvent, source: Source) => {
    event.preventDefault();
    setBusy(`${source.id}:edit`);
    setError('');
    setMessage('');
    try {
      const response = await fetch(`/api/webdav/${source.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sourcePayload(editForm)),
      });
      if (!response.ok) throw new Error(await readError(response, '保存连接器失败'));
      const result = (await response.json()) as { requires_rescan?: boolean };
      setEditingSourceId(null);
      setMessage(
        result.requires_rescan
          ? `连接器“${editForm.name}”已更新。扫描范围发生变化，请测试连接后重新扫描。`
          : `连接器“${editForm.name}”已更新，已保存的密码和入库资料不受影响。`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存连接器失败');
    } finally {
      setBusy('');
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
    <main className="mx-auto max-w-5xl px-4 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">设置</h1>
      <p className="mt-2 text-sm text-slate-500">
        管理模型、知识来源和系统处理能力。日常整理请前往知识库或收件箱。
      </p>
      <SettingsSectionNav active="sources" />

      <div className="mt-8">
        <p className="text-xs font-semibold uppercase tracking-wide text-emerald-600">外部知识来源</p>
        <h2 className="mt-1 text-xl font-semibold text-slate-900">知识源</h2>
      </div>
      <p className="mt-1 text-sm text-slate-500">
        连接外部文件夹，藏知只读扫描远端文件，下载后沿用正文解析、智能切片和向量流程。
      </p>

      <details className="mt-6 rounded-2xl border bg-white">
        <summary className="flex cursor-pointer items-center justify-between gap-4 px-5 py-4 hover:bg-slate-50">
          <div>
            <h2 className="font-semibold text-slate-900">添加 WebDAV 文件夹</h2>
            <p className="mt-1 text-xs text-slate-500">连接新的只读远端目录</p>
          </div>
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-slate-100 text-lg text-slate-500">＋</span>
        </summary>
        <form onSubmit={create} className="border-t border-slate-100 px-5 pb-5">
          <SourceFields form={form} setForm={setForm} />
          <button disabled={creating} className="mt-4 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
            {creating ? '正在添加…' : '添加知识源'}
          </button>
        </form>
      </details>

      {message && <p className="mt-4 rounded bg-emerald-50 p-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="mt-4 rounded bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      <div className="mt-6 space-y-4">
        {sources.map((source) => (
          <section key={source.id} className="rounded-2xl border bg-white p-5">
            {editingSourceId === source.id ? (
              <form onSubmit={(event) => void save(event, source)}>
                <div className="flex items-center justify-between gap-3">
                  <h2 className="font-semibold">编辑 WebDAV 连接器</h2>
                  <span className="text-xs text-slate-500">
                    {source.has_password ? '已保存密码' : '未保存密码'}
                  </span>
                </div>
                <SourceFields
                  form={editForm}
                  setForm={setEditForm}
                  editing
                  hasPassword={source.has_password}
                />
                <div className="mt-4 flex gap-2">
                  <button
                    disabled={Boolean(busy)}
                    className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
                  >
                    {busy === `${source.id}:edit` ? '保存中…' : '保存修改'}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => setEditingSourceId(null)}
                    className="rounded border border-slate-300 px-4 py-2 text-sm"
                  >
                    取消
                  </button>
                </div>
              </form>
            ) : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">{source.name}</h2>
                    <p className="mt-1 break-all text-xs text-slate-500">{source.base_url}{source.root_path}</p>
                    <p className="mt-2 text-sm text-slate-600">
                      文件 {source.entry_counts.total} · 已同步 {source.entry_counts.synced} · 待同步 {source.entry_counts.pending} · 失败 {source.entry_counts.failed}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      {source.username || '匿名访问'} · {source.recursive ? '包含子目录' : '仅当前目录'} · {source.include_extensions.join('、') || '未配置文件类型'}
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
                  <button type="button" disabled={Boolean(busy)} onClick={() => startEditing(source)} className="rounded px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-40">
                    编辑配置
                  </button>
                  <button type="button" onClick={() => void showEntries(source.id).catch((caught) => setError(caught instanceof Error ? caught.message : '读取失败'))} className="rounded px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100">
                    {entries[source.id] ? '收起清单' : '查看文件'}
                  </button>
                  <button type="button" disabled={Boolean(busy)} onClick={() => void remove(source)} className="rounded px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40">
                    {busy === `${source.id}:delete` ? '删除中…' : '删除连接器'}
                  </button>
                </div>
                {source.last_error && <p className="mt-3 text-sm text-red-700">{source.last_error}</p>}
                {entries[source.id] && <EntryTable entries={entries[source.id]} />}
              </>
            )}
          </section>
        ))}
        {!sources.length && <p className="rounded-xl border border-dashed p-8 text-center text-sm text-slate-500">还没有知识源。</p>}
      </div>
    </main>
  );
}

function SourceFields({
  form,
  setForm,
  editing = false,
  hasPassword = false,
}: {
  form: SourceForm;
  setForm: (form: SourceForm) => void;
  editing?: boolean;
  hasPassword?: boolean;
}) {
  return (
    <div className="mt-4 grid gap-3 md:grid-cols-2">
      <Field label="名称" value={form.name} onChange={(name) => setForm({ ...form, name })} />
      <Field label="WebDAV 地址" value={form.base_url} onChange={(base_url) => setForm({ ...form, base_url })} placeholder="https://example.com/dav/" />
      <Field label="用户名" required={false} value={form.username} onChange={(username) => setForm({ ...form, username })} />
      <Field
        label={editing ? '新密码（留空保留原密码）' : '密码'}
        type="password"
        required={false}
        disabled={form.clear_password}
        value={form.password}
        onChange={(password) => setForm({ ...form, password })}
      />
      <Field label="同步目录" value={form.root_path} onChange={(root_path) => setForm({ ...form, root_path })} />
      <Field
        label="纳入的文件类型"
        value={form.include_extensions}
        onChange={(include_extensions) => setForm({ ...form, include_extensions })}
        placeholder=".pdf, .docx, .md, .txt"
      />
      <label className="text-sm text-slate-700 md:col-span-2">
        忽略规则（每行一条）
        <textarea
          value={form.ignore_patterns}
          onChange={(event) => setForm({ ...form, ignore_patterns: event.target.value })}
          rows={3}
          className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 font-mono text-xs"
        />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={form.recursive} onChange={(event) => setForm({ ...form, recursive: event.target.checked })} />
        扫描同步目录下的子目录
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={form.trusted_private_network} onChange={(event) => setForm({ ...form, trusted_private_network: event.target.checked })} />
        允许连接可信内网地址
      </label>
      {editing && hasPassword && (
        <label className="flex items-center gap-2 text-sm text-red-700 md:col-span-2">
          <input
            type="checkbox"
            checked={form.clear_password}
            onChange={(event) => setForm({ ...form, clear_password: event.target.checked, password: '' })}
          />
          清除已保存的密码（仅匿名 WebDAV 使用）
        </label>
      )}
    </div>
  );
}

function Field({ label, value, onChange, type = 'text', placeholder = '', required = true, disabled = false }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string; required?: boolean; disabled?: boolean }) {
  return <label className="text-sm text-slate-700">{label}<input required={required} disabled={disabled} value={value} type={type} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 disabled:bg-slate-100" /></label>;
}

function EntryTable({ entries }: { entries: Entry[] }) {
  return <div className="mt-4 overflow-x-auto border-t pt-3"><table className="min-w-full text-left text-xs"><thead className="text-slate-500"><tr><th className="py-2">远端文件</th><th>状态</th><th>大小</th><th>资料</th></tr></thead><tbody>{entries.map((entry) => <tr key={entry.id} className="border-t"><td className="max-w-xl break-all py-2 pr-4">{entry.remote_path}</td><td>{entryState(entry.state)}</td><td>{formatSize(entry.file_size)}</td><td>{entry.document_id ? <Link className="text-blue-700 hover:underline" href={`/documents/${entry.document_id}`}>查看</Link> : '—'}</td></tr>)}</tbody></table></div>;
}

function statusLabel(value: string) {
  return ({ idle: '空闲', scanning: '扫描中', syncing: '同步中', failed: '有失败' } as Record<string, string>)[value] ?? value;
}
function entryState(value: string) {
  return ({ discovered: '待同步', changed: '有更新', synced: '已同步', failed: '失败', missing: '远端已删除', ignored: '已从知识库排除' } as Record<string, string>)[value] ?? value;
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

function sourcePayload(form: SourceForm) {
  return {
    ...form,
    include_extensions: splitExtensions(form.include_extensions),
    ignore_patterns: splitRules(form.ignore_patterns),
  };
}

function splitExtensions(value: string) {
  return [...new Set(value.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}

function splitRules(value: string) {
  return [...new Set(value.split(/[\n,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}
