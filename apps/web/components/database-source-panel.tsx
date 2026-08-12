'use client';

import { FormEvent, useCallback, useEffect, useState } from 'react';

import {
  DatabaseCatalogTable,
  DatabaseDeleteImpact,
  DatabaseSource,
  createDatabaseSource,
  deleteDatabaseSource,
  fetchDatabaseCatalog,
  fetchDatabaseDeleteImpact,
  fetchDatabaseSchemas,
  fetchDatabaseSources,
  importDatabaseTable,
  testDatabaseSource,
  updateDatabaseSource,
} from '@/lib/api';

const ENGINE_PORTS: Record<string, number> = {
  postgresql: 5432,
  mysql: 3306,
};

const PG_SSL_MODES = ['prefer', 'disable', 'require'];
const MYSQL_SSL_MODES = ['disabled', 'required'];

const SSL_LABELS: Record<string, string> = {
  prefer: '优先（服务器支持时加密）',
  preferred: '优先（服务器支持时加密）',
  disable: '禁用 TLS',
  disabled: '禁用 TLS',
  allow: '允许但不要求',
  require: '必须加密',
  required: '必须加密',
  'verify-ca': '加密并校验服务器证书',
  verify_ca: '加密并校验服务器证书',
  'verify-full': '加密并校验证书与主机名',
  verify_identity: '加密并校验证书与身份',
};

const ENGINE_LABELS: Record<string, string> = {
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
};

type DbForm = {
  name: string;
  engine: 'postgresql' | 'mysql';
  host: string;
  port: number;
  database_name: string;
  username: string;
  password: string;
  password_action: 'keep' | 'replace' | 'clear';
  ssl_mode: string;
  trusted_private_network: boolean;
};

function emptyForm(): DbForm {
  return {
    name: '',
    engine: 'postgresql',
    host: '',
    port: ENGINE_PORTS.postgresql,
    database_name: '',
    username: '',
    password: '',
    password_action: 'replace',
    ssl_mode: 'prefer',
    trusted_private_network: false,
  };
}

type BrowseState = {
  schemas: string[];
  schema: string | null;
  tables: DatabaseCatalogTable[];
  loadingTab: boolean;
  error: string | null;
  expanded: string | null;
};

type RemoveDialog = {
  source: DatabaseSource;
  impact: DatabaseDeleteImpact;
  documentAction: 'keep' | 'trash';
};

export function DatabaseSourcePanel() {
  const [sources, setSources] = useState<DatabaseSource[]>([]);
  const [creating, setCreating] = useState(false);
  const [createForm, setCreateForm] = useState<DbForm>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<DbForm>(emptyForm);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [removeDialog, setRemoveDialog] = useState<RemoveDialog | null>(null);
  const [browse, setBrowse] = useState<Record<number, BrowseState>>({});

  const load = useCallback(async () => {
    setSources(await fetchDatabaseSources());
  }, []);

  useEffect(() => {
    void load().catch((caught) =>
      setError(caught instanceof Error ? caught.message : '数据库来源读取失败'),
    );
  }, [load]);

  const startCreate = () => {
    setCreateForm(emptyForm());
    setCreating(true);
    setMessage('');
    setError('');
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    setBusy('create');
    setError('');
    try {
      const { name, engine, host, port, database_name, username, password, ssl_mode, trusted_private_network } = createForm;
      await createDatabaseSource({
        name,
        engine,
        host,
        port,
        database_name,
        username,
        password,
        ssl_mode,
        trusted_private_network,
      });
      setCreateForm(emptyForm());
      setCreating(false);
      setMessage(`数据库来源“${createForm.name}”已添加，建议先测试连接再导入快照。`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '添加数据库来源失败');
    } finally {
      setBusy('');
    }
  };

  const startEditing = (source: DatabaseSource) => {
    setEditForm({
      name: source.name,
      engine: source.engine,
      host: source.host,
      port: source.port,
      database_name: source.database_name,
      username: source.username,
      password: '',
      password_action: 'keep',
      ssl_mode: source.ssl_mode,
      trusted_private_network: source.trusted_private_network,
    });
    setEditingId(source.id);
    setMessage('');
    setError('');
  };

  const save = async (event: FormEvent, source: DatabaseSource) => {
    event.preventDefault();
    setBusy(`${source.id}:edit`);
    setError('');
    try {
      const payload: Record<string, unknown> = {
        name: editForm.name,
        host: editForm.host,
        port: editForm.port,
        database_name: editForm.database_name,
        username: editForm.username,
        ssl_mode: editForm.ssl_mode,
        trusted_private_network: editForm.trusted_private_network,
      };
      if (editForm.password_action === 'replace') {
        payload.password_action = 'replace';
        payload.password = editForm.password;
      } else if (editForm.password_action === 'clear') {
        payload.password_action = 'clear';
      } else {
        payload.password = '';
        payload.password_action = 'keep';
      }
      await updateDatabaseSource(source.id, payload);
      setEditingId(null);
      setMessage(`数据库来源“${editForm.name}”已更新。`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存数据库来源失败');
    } finally {
      setBusy('');
    }
  };

  const runTest = async (source: DatabaseSource) => {
    setBusy(`${source.id}:test`);
    setError('');
    try {
      const result = await testDatabaseSource(source.id);
      setMessage(
        `${source.name}：连接正常${result.server_version ? `（${result.server_version}）` : ''}${result.current_database ? `，当前库 ${result.current_database}` : ''}`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '测试连接失败');
      await load();
    } finally {
      setBusy('');
    }
  };

  const toggleEnabled = async (source: DatabaseSource) => {
    setBusy(`${source.id}:enabled`);
    setError('');
    try {
      await updateDatabaseSource(source.id, { is_enabled: !source.is_enabled });
      setMessage(
        !source.is_enabled
          ? `数据库来源“${source.name}”已启用。`
          : `数据库来源“${source.name}”已停用，配置和已导入快照均保留。`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '状态修改失败');
    } finally {
      setBusy('');
    }
  };

  const prepareRemove = async (source: DatabaseSource) => {
    setBusy(`${source.id}:impact`);
    setError('');
    try {
      const impact = await fetchDatabaseDeleteImpact(source.id);
      setRemoveDialog({ source, impact, documentAction: 'keep' });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除影响核对失败');
    } finally {
      setBusy('');
    }
  };

  const remove = async (dialog: RemoveDialog) => {
    const { source, documentAction } = dialog;
    setBusy(`${source.id}:delete`);
    setError('');
    try {
      const result = await deleteDatabaseSource(source.id, documentAction);
      setMessage(result.message || `数据库来源“${source.name}”已删除。`);
      setRemoveDialog(null);
      setBrowse((current) => {
        const next = { ...current };
        delete next[source.id];
        return next;
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除数据库来源失败');
    } finally {
      setBusy('');
    }
  };

  const loadCatalog = async (sourceId: number, schema: string) => {
    setBusy(`${sourceId}:catalog`);
    setError('');
    try {
      const tables = await fetchDatabaseCatalog(sourceId, schema);
      setBrowse((current) => ({
        ...current,
        [sourceId]: {
          ...current[sourceId],
          schema,
          tables,
          loadingTab: false,
          error: null,
        },
      }));
    } catch (caught) {
      setBrowse((current) => ({
        ...current,
        [sourceId]: {
          ...current[sourceId],
          schema,
          loadingTab: false,
          error: caught instanceof Error ? caught.message : '读取表结构失败',
        },
      }));
    } finally {
      setBusy('');
    }
  };

  const toggleBrowse = async (source: DatabaseSource) => {
    if (browse[source.id]) {
      setBrowse((current) => {
        const next = { ...current };
        delete next[source.id];
        return next;
      });
      return;
    }
    setBusy(`${source.id}:browse`);
    setError('');
    try {
      const schemas = await fetchDatabaseSchemas(source.id);
      setBrowse((current) => ({
        ...current,
        [source.id]: {
          schemas,
          schema: schemas[0] ?? null,
          tables: [],
          loadingTab: false,
          error: null,
          expanded: null,
        },
      }));
      if (schemas[0]) await loadCatalog(source.id, schemas[0]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '读取 Schema 失败');
    } finally {
      setBusy('');
    }
  };

  const selectSchema = async (sourceId: number, schema: string) => {
    setBrowse((current) => ({
      ...current,
      [sourceId]: { ...current[sourceId], schema, loadingTab: true, error: null },
    }));
    await loadCatalog(sourceId, schema);
  };

  const runImport = async (source: DatabaseSource, table: DatabaseCatalogTable) => {
    if (!browse[source.id]?.schema) return;
    setBusy(`${source.id}:import:${table.schema_name}.${table.table_name}`);
    setError('');
    try {
      const result = await importDatabaseTable(source.id, table.schema_name, table.table_name);
      setMessage(
        `已导入 ${table.schema_name}.${table.table_name} 的快照：${result.row_count} 行 × ${result.column_count} 列${result.reused_document ? '（更新已有资料）' : '（新资料）'}，可在数据集、API 与 MCP 中查询。`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '导入快照失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="mt-6">
      <div className="rounded-2xl border border-blue-200 bg-blue-50/60 p-4 text-sm leading-6 text-blue-900/80">
        <p className="font-semibold text-blue-950">
          数据库连接器为<strong>只读快照</strong>，绝不修改远程数据库。
        </p>
        <p className="mt-1">
          建立连接后，可选择单张表导入为本地快照，导入后在数据集、API 与 MCP 中即可查询。首版单表上限{' '}
          <strong>10 万行</strong>；重复导入同一张表会更新已有资料，不会重复新增。
        </p>
      </div>

      {message && <p className="mt-4 rounded bg-emerald-50 p-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="mt-4 rounded bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      <button
        type="button"
        onClick={startCreate}
        className="mt-5 rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-800"
      >
        ＋ 添加数据库来源
      </button>

      {creating && (
        <section className="mt-4 rounded-2xl border bg-white p-5">
          <h2 className="font-semibold text-slate-900">添加数据库来源</h2>
          <form onSubmit={(event) => void create(event)} className="mt-2">
            <DbFields form={createForm} setForm={setCreateForm} />
            <div className="mt-4 flex gap-2">
              <button
                disabled={Boolean(busy)}
                className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {busy === 'create' ? '正在添加…' : '添加数据库来源'}
              </button>
              <button
                type="button"
                disabled={Boolean(busy)}
                onClick={() => setCreating(false)}
                className="rounded-xl border border-slate-300 px-4 py-2 text-sm"
              >
                取消
              </button>
            </div>
          </form>
        </section>
      )}

      <div className="mt-5 space-y-4">
        {sources.map((source) => (
          <section key={source.id} className="rounded-2xl border bg-white p-5">
            {editingId === source.id ? (
              <form onSubmit={(event) => void save(event, source)}>
                <div className="flex items-center justify-between gap-3">
                  <h2 className="font-semibold">编辑数据库来源</h2>
                  <span className="text-xs text-slate-500">
                    {source.has_password ? '已保存密码' : '未保存密码'}
                  </span>
                </div>
                <DbFields
                  form={editForm}
                  setForm={setEditForm}
                  editing
                  hasPassword={source.has_password}
                />
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    disabled={Boolean(busy)}
                    className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {busy === `${source.id}:edit` ? '保存中…' : '保存修改'}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => setEditingId(null)}
                    className="rounded-xl border border-slate-300 px-4 py-2 text-sm"
                  >
                    取消
                  </button>
                </div>
              </form>
            ) : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="font-semibold">{source.name}</h2>
                    <p className="mt-1 break-all text-xs text-slate-500">
                      {(ENGINE_LABELS[source.engine] ?? source.engine)} · {source.host}:{source.port} ·{' '}
                      {source.database_name}
                    </p>
                    <p className="mt-2 text-sm text-slate-600">
                      已导入快照 {source.snapshot_counts.total} 张
                      {source.last_tested_at
                        ? ` · 最近测试 ${formatTime(source.last_tested_at)}`
                        : ''}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      {source.username || '无用户名'} · {SSL_LABELS[source.ssl_mode] ??
                      source.ssl_mode} · {source.trusted_private_network ? '允许可信内网' : '仅公网地址'}
                    </p>
                  </div>
                  <span
                    className={`rounded-full px-3 py-1 text-xs ${
                      source.is_enabled
                        ? 'bg-emerald-50 text-emerald-700'
                        : 'bg-slate-100 text-slate-500'
                    }`}
                  >
                    {source.is_enabled ? statusLabel(source.status) : '已停用'}
                  </span>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => void runTest(source)}
                    className="rounded-xl border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                  >
                    {busy === `${source.id}:test` ? '测试中…' : '测试连接'}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy) || !source.is_enabled}
                    onClick={() => void toggleBrowse(source)}
                    className="rounded-xl border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                  >
                    {busy === `${source.id}:browse` || busy === `${source.id}:catalog`
                      ? '读取中…'
                      : browse[source.id]
                        ? '收起浏览'
                        : '浏览并导入表'}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => startEditing(source)}
                    className="rounded px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-40"
                  >
                    编辑配置
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => void toggleEnabled(source)}
                    className="rounded px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                  >
                    {busy === `${source.id}:enabled` ? '处理中…' : source.is_enabled ? '停用' : '启用'}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => void prepareRemove(source)}
                    className="rounded px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40"
                  >
                    {busy === `${source.id}:impact` ? '正在核对影响…' : '删除连接器'}
                  </button>
                </div>
                {source.last_error && <p className="mt-3 text-sm text-red-700">{source.last_error}</p>}
                {browse[source.id] && (
                  <BrowseTables
                    state={browse[source.id]}
                    source={source}
                    busy={busy}
                    onSelectSchema={(schema) => void selectSchema(source.id, schema)}
                    onImport={(table) => void runImport(source, table)}
                    onToggleExpand={(key) =>
                      setBrowse((current) => ({
                        ...current,
                        [source.id]: {
                          ...current[source.id],
                          expanded: current[source.id].expanded === key ? null : key,
                        },
                      }))
                    }
                  />
                )}
              </>
            )}
          </section>
        ))}
        {!sources.length && (
          <p className="rounded-xl border border-dashed p-8 text-center text-sm text-slate-500">
            还没有数据库来源。
          </p>
        )}
      </div>

      {removeDialog && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4">
          <div role="dialog" aria-modal="true" className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
            <h2 className="text-lg font-semibold text-slate-900">
              删除数据库来源“{removeDialog.source.name}”
            </h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">
              将删除连接配置和 {removeDialog.impact.snapshot_count} 张已导入快照的关联记录。远程数据库
              保持只读，不会被修改或删除。
            </p>
            <fieldset className="mt-5 space-y-3">
              <label className="flex cursor-pointer gap-3 rounded-xl border border-slate-200 p-4">
                <input
                  type="radio"
                  name="database-document-action"
                  checked={removeDialog.documentAction === 'keep'}
                  onChange={() => setRemoveDialog({ ...removeDialog, documentAction: 'keep' })}
                />
                <span>
                  <span className="block text-sm font-medium text-slate-900">保留已导入资料（推荐）</span>
                  <span className="mt-1 block text-xs leading-5 text-slate-500">
                    {removeDialog.impact.active_document_count} 条可用资料继续保留在知识库，只是与连接器脱离。
                  </span>
                </span>
              </label>
              <label className="flex cursor-pointer gap-3 rounded-xl border border-slate-200 p-4">
                <input
                  type="radio"
                  name="database-document-action"
                  checked={removeDialog.documentAction === 'trash'}
                  onChange={() => setRemoveDialog({ ...removeDialog, documentAction: 'trash' })}
                />
                <span>
                  <span className="block text-sm font-medium text-slate-900">将已导入资料移入回收站</span>
                  <span className="mt-1 block text-xs leading-5 text-slate-500">
                    以后仍可恢复；要彻底清除内容请再到回收站永久删除。
                  </span>
                </span>
              </label>
            </fieldset>
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                disabled={Boolean(busy)}
                onClick={() => setRemoveDialog(null)}
                className="rounded-xl border border-slate-300 px-4 py-2 text-sm"
              >
                取消
              </button>
              <button
                type="button"
                disabled={Boolean(busy)}
                onClick={() => void remove(removeDialog)}
                className="rounded-xl bg-red-700 px-4 py-2 text-sm text-white disabled:opacity-40"
              >
                {busy === `${removeDialog.source.id}:delete` ? '正在删除…' : '确认删除连接器'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DbFields({
  form,
  setForm,
  editing = false,
  hasPassword = false,
}: {
  form: DbForm;
  setForm: (form: DbForm) => void;
  editing?: boolean;
  hasPassword?: boolean;
}) {
  const changeEngine = (engine: 'postgresql' | 'mysql') => {
    setForm({
      ...form,
      engine,
      port: ENGINE_PORTS[engine],
      ssl_mode: engine === 'postgresql' ? 'prefer' : 'required',
    });
  };
  const sslModes = form.engine === 'postgresql' ? PG_SSL_MODES : MYSQL_SSL_MODES;
  return (
    <div className="mt-4 grid gap-3 md:grid-cols-2">
      <Field label="名称" value={form.name} onChange={(name) => setForm({ ...form, name })} />
      <label className="text-sm text-slate-700">
        数据库类型
        <select
          value={form.engine}
          onChange={(event) => changeEngine(event.target.value as 'postgresql' | 'mysql')}
          className="mt-1 block w-full rounded border border-slate-300 bg-white px-3 py-2"
        >
          <option value="postgresql">PostgreSQL</option>
          <option value="mysql">MySQL</option>
        </select>
      </label>
      <Field label="主机地址" value={form.host} onChange={(host) => setForm({ ...form, host })} placeholder="db.example.com" />
      <Field
        label={`端口（${ENGINE_PORTS[form.engine]} 为默认）`}
        type="number"
        value={String(form.port)}
        onChange={(port) => setForm({ ...form, port: Number(port) || 0 })}
      />
      <Field label="数据库名" value={form.database_name} onChange={(database_name) => setForm({ ...form, database_name })} />
      <Field label="用户名" required={false} value={form.username} onChange={(username) => setForm({ ...form, username })} />
      <PasswordFields form={form} setForm={setForm} editing={editing} hasPassword={hasPassword} />
      <label className="text-sm text-slate-700">
        SSL / TLS 模式
        <select
          value={form.ssl_mode}
          onChange={(event) => setForm({ ...form, ssl_mode: event.target.value })}
          className="mt-1 block w-full rounded border border-slate-300 bg-white px-3 py-2"
        >
          {sslModes.map((mode) => (
            <option key={mode} value={mode}>
              {SSL_LABELS[mode] ?? mode}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm md:col-span-2">
        <input
          type="checkbox"
          checked={form.trusted_private_network}
          onChange={(event) => setForm({ ...form, trusted_private_network: event.target.checked })}
        />
        允许连接可信内网地址
      </label>
      <p className="text-xs leading-5 text-slate-500 md:col-span-2">
        为安全起见，本机、链路本地及未指定的回环地址始终被拒绝；仅当勾选“可信内网”时才允许连接非公网地址。
      </p>
    </div>
  );
}

function PasswordFields({
  form,
  setForm,
  editing,
  hasPassword,
}: {
  form: DbForm;
  setForm: (form: DbForm) => void;
  editing: boolean;
  hasPassword: boolean;
}) {
  if (!editing) {
    return (
      <Field
        label="密码"
        type="password"
        required={false}
        value={form.password}
        onChange={(password) => setForm({ ...form, password })}
      />
    );
  }
  return (
    <div className="md:col-span-2">
      <label className="text-sm text-slate-700">
        密码处理
        <select
          value={form.password_action}
          onChange={(event) =>
            setForm({
              ...form,
              password_action: event.target.value as 'keep' | 'replace' | 'clear',
              password: '',
            })
          }
          className="mt-1 block w-full rounded border border-slate-300 bg-white px-3 py-2"
        >
          <option value="keep">保留已保存的密码</option>
          <option value="replace">替换为新密码</option>
          {hasPassword && <option value="clear">清除密码</option>}
        </select>
      </label>
      {form.password_action === 'replace' && (
        <Field
          label="新密码"
          type="password"
          required={false}
          value={form.password}
          onChange={(password) => setForm({ ...form, password })}
        />
      )}
      {form.password_action === 'clear' && (
        <p className="mt-1 text-xs leading-5 text-slate-500">
          清除后将以无密码方式连接（仅适用于允许匿名/无密码的数据库）。
        </p>
      )}
    </div>
  );
}

function BrowseTables({
  state,
  source,
  busy,
  onSelectSchema,
  onImport,
  onToggleExpand,
}: {
  state: BrowseState;
  source: DatabaseSource;
  busy: string;
  onSelectSchema: (schema: string) => void;
  onImport: (table: DatabaseCatalogTable) => void;
  onToggleExpand: (key: string | null) => void;
}) {
  return (
    <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-slate-500">Schema</label>
        <select
          value={state.schema ?? ''}
          onChange={(event) => onSelectSchema(event.target.value)}
          className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          {(state.schemas || []).map((schema) => (
            <option key={schema} value={schema}>
              {schema}
            </option>
          ))}
        </select>
        {state.loadingTab && <span className="text-xs text-slate-500">读取表结构中…</span>}
      </div>
      {state.error && <p className="mt-3 text-sm text-red-700">{state.error}</p>}
      {!state.error && state.schema && !state.loadingTab && (
        <ul className="mt-3 space-y-2">
          {state.tables.map((table) => {
            const key = `${table.schema_name}.${table.table_name}`;
            const expanded = state.expanded === key;
            return (
              <li key={key} className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-slate-900">
                      {table.table_name}
                      <span className="ml-2 text-xs font-normal text-slate-400">
                        {table.kind === 'view' ? '视图' : '表'} · {table.columns.length} 列
                      </span>
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => onToggleExpand(expanded ? null : key)}
                      className="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
                    >
                      {expanded ? '收起列信息' : '查看列信息'}
                    </button>
                    <button
                      type="button"
                      onClick={() => onImport(table)}
                      disabled={Boolean(busy)}
                      className="rounded-xl bg-slate-950 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                    >
                      {busy === `${source.id}:import:${key}` ? '导入中…' : '导入快照'}
                    </button>
                  </div>
                </div>
                {expanded && (
                  <div className="mt-2 overflow-x-auto">
                    <table className="min-w-full text-left text-xs">
                      <thead className="text-slate-500">
                        <tr>
                          <th className="py-1 pr-3">列名</th>
                          <th className="pr-3">类型</th>
                          <th className="pr-3">可空</th>
                          <th>主键</th>
                        </tr>
                      </thead>
                      <tbody>
                        {table.columns.map((column) => (
                          <tr key={column.name} className="border-t">
                            <td className="py-1 pr-3 font-medium text-slate-800">{column.name}</td>
                            <td className="pr-3 text-slate-500">{column.data_type}</td>
                            <td className="pr-3 text-slate-500">{column.nullable ? '是' : '否'}</td>
                            <td className="text-slate-500">{column.is_primary_key ? '是' : '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </li>
            );
          })}
          {!state.tables.length && (
            <p className="p-4 text-center text-sm text-slate-500">该 Schema 下没有可导入的表。</p>
          )}
        </ul>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder = '',
  required = true,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="text-sm text-slate-700">
      {label}
      <input
        required={required}
        value={value}
        type={type}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 block w-full rounded border border-slate-300 px-3 py-2"
      />
    </label>
  );
}

function statusLabel(value: string) {
  return ({ idle: '空闲', failed: '连接失败' } as Record<string, string>)[value] ?? value;
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
