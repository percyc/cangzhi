'use client';

import { useRouter } from 'next/navigation';
import {
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { SettingsSectionNav } from '@/components/SettingsSectionNav';

type Workspace = {
  slug: string;
  name: string;
  description: string | null;
  is_default: boolean;
  status: 'active' | 'archived';
  settings: Record<string, unknown>;
};

type CreateForm = {
  name: string;
  slug: string;
  description: string;
};

type EditForm = {
  name: string;
  description: string;
};

const EMPTY_CREATE: CreateForm = { name: '', slug: '', description: '' };
const EMPTY_EDIT: EditForm = { name: '', description: '' };

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

export default function WorkspacesSettingsPage() {
  const router = useRouter();
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [current, setCurrent] = useState<Workspace | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [createForm, setCreateForm] = useState<CreateForm>(EMPTY_CREATE);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const slugManuallyEdited = useRef(false);

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditForm>(EMPTY_EDIT);
  const [savingEdit, setSavingEdit] = useState(false);

  const [archivingSlug, setArchivingSlug] = useState<string | null>(null);
  const [restoringSlug, setRestoringSlug] = useState<string | null>(null);
  const [switchingSlug, setSwitchingSlug] = useState<string | null>(null);

  const inFlight =
    creating ||
    savingEdit ||
    archivingSlug !== null ||
    restoringSlug !== null ||
    switchingSlug !== null;

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchJson<Workspace[]>('/api/workspaces', '工作空间列表读取失败'),
      fetchOptional<Workspace>('/api/workspaces/current'),
    ])
      .then(([list, currentWorkspace]) => {
        if (cancelled) return;
        setWorkspaces(list);
        setCurrent(currentWorkspace);
        setLoadError(null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setLoadError(
          caught instanceof Error ? caught.message : '工作空间列表读取失败',
        );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const { active, archived } = useMemo(() => {
    if (!workspaces) {
      return { active: [], archived: [] };
    }
    return {
      active: workspaces.filter((workspace) => workspace.status === 'active'),
      archived: workspaces.filter((workspace) => workspace.status === 'archived'),
    };
  }, [workspaces]);

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    if (creating) return;
    const name = createForm.name.trim();
    const slug = createForm.slug.trim();
    const description = createForm.description.trim();
    if (!name) {
      setCreateError('请输入工作空间名称');
      return;
    }
    if (!slug) {
      setCreateError('请输入标识（slug）');
      return;
    }
    if (slug.length > 64 || !SLUG_PATTERN.test(slug)) {
      setCreateError('标识需以字母或数字开头，仅含小写字母、数字或连字符，最长 64 个字符');
      return;
    }
    setCreating(true);
    setCreateError(null);
    setError(null);
    setMessage(null);
    try {
      const created = await postJson<Workspace>('/api/workspaces', {
        slug,
        name,
        description: description || null,
      });
      setWorkspaces((current) => {
        if (!current) return [created];
        return [...current, created];
      });
      setCreateForm(EMPTY_CREATE);
      slugManuallyEdited.current = false;
      setMessage(`已创建工作空间 “${created.name}”。`);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : '创建工作空间失败');
    } finally {
      setCreating(false);
    }
  };

  const handleCreateNameChange = (next: string) => {
    setCreateForm((current) => {
      const shouldAutoSlug =
        !slugManuallyEdited.current || current.slug.trim() === '';
      const derived = shouldAutoSlug ? deriveSlug(next) : current.slug;
      return { ...current, name: next, slug: derived };
    });
  };

  const handleCreateSlugChange = (next: string) => {
    slugManuallyEdited.current = true;
    setCreateForm((current) => ({ ...current, slug: next }));
  };

  const startEdit = (workspace: Workspace) => {
    setEditingSlug(workspace.slug);
    setEditForm({
      name: workspace.name,
      description: workspace.description ?? '',
    });
    setError(null);
    setMessage(null);
  };

  const cancelEdit = () => {
    setEditingSlug(null);
    setEditForm(EMPTY_EDIT);
  };

  const saveEdit = async (event: FormEvent, workspace: Workspace) => {
    event.preventDefault();
    if (savingEdit) return;
    const name = editForm.name.trim();
    const description = editForm.description.trim();
    if (!name) {
      setError('名称不能为空');
      return;
    }
    setSavingEdit(true);
    setError(null);
    setMessage(null);
    try {
      const updated = await patchJson<Workspace>(
        `/api/workspaces/${encodeURIComponent(workspace.slug)}`,
        {
          name,
          description: description ? description : null,
        },
      );
      setWorkspaces((current) =>
        current
          ? current.map((item) => (item.slug === workspace.slug ? updated : item))
          : current,
      );
      if (current && current.slug === workspace.slug) {
        setCurrent(updated);
      }
      setEditingSlug(null);
      setEditForm(EMPTY_EDIT);
      setMessage(`已保存 “${updated.name}” 的修改。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存工作空间失败');
    } finally {
      setSavingEdit(false);
    }
  };

  const requestArchive = (workspace: Workspace) => {
    if (workspace.is_default) return;
    if (archivingSlug) return;
    const confirmed = window.confirm(
      `归档工作空间 “${workspace.name}” 吗？归档后该空间的资料仍会保留，但不再出现在活跃空间列表，可随时在下方“已归档”中还原。`,
    );
    if (!confirmed) return;
    void archive(workspace);
  };

  const archive = async (workspace: Workspace) => {
    setArchivingSlug(workspace.slug);
    setError(null);
    setMessage(null);
    try {
      const updated = await postJson<Workspace>(
        `/api/workspaces/${encodeURIComponent(workspace.slug)}/archive`,
        {},
      );
      setWorkspaces((current) =>
        current
          ? current.map((item) => (item.slug === workspace.slug ? updated : item))
          : current,
      );
      if (current && current.slug === workspace.slug) {
        setCurrent(updated);
      }
      if (current && current.slug === workspace.slug) {
        const defaultWorkspace = workspaces?.find((item) => item.is_default);
        setWorkspaceCookie('default');
        setCurrent(defaultWorkspace ?? null);
        setMessage(`已归档 “${updated.name}”，并切换到默认空间。`);
      } else {
        setMessage(`已归档 “${updated.name}”。`);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '归档工作空间失败');
    } finally {
      setArchivingSlug(null);
    }
  };

  const restore = async (workspace: Workspace) => {
    if (restoringSlug) return;
    setRestoringSlug(workspace.slug);
    setError(null);
    setMessage(null);
    try {
      const updated = await postJson<Workspace>(
        `/api/workspaces/${encodeURIComponent(workspace.slug)}/restore`,
        {},
      );
      setWorkspaces((current) =>
        current
          ? current.map((item) => (item.slug === workspace.slug ? updated : item))
          : current,
      );
      setMessage(`已还原 “${updated.name}”。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '还原工作空间失败');
    } finally {
      setRestoringSlug(null);
    }
  };

  const switchTo = (workspace: Workspace) => {
    if (switchingSlug) return;
    if (current && current.slug === workspace.slug) return;
    setSwitchingSlug(workspace.slug);
    setWorkspaceCookie(workspace.slug);
    router.push('/documents');
  };

  if (loadError && workspaces === null) {
    return (
      <main className="mx-auto max-w-5xl px-5 py-9">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
          系统设置
        </p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">
          工作空间
        </h1>
        <SettingsSectionNav active="workspaces" />
        <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-800">
          <p>读取工作空间失败：{loadError}</p>
          <button
            type="button"
            onClick={() => {
              setLoadError(null);
              setWorkspaces(null);
              const cancelled = false;
              Promise.all([
                fetchJson<Workspace[]>('/api/workspaces', '工作空间列表读取失败'),
                fetchOptional<Workspace>('/api/workspaces/current'),
              ])
                .then(([list, currentWorkspace]) => {
                  if (cancelled) return;
                  setWorkspaces(list);
                  setCurrent(currentWorkspace);
                })
                .catch((caught: unknown) => {
                  if (cancelled) return;
                  setLoadError(
                    caught instanceof Error
                      ? caught.message
                      : '工作空间列表读取失败',
                  );
                });
            }}
            className="mt-3 rounded-xl border border-red-200 bg-white px-4 py-2 text-sm text-red-700 hover:bg-red-50"
          >
            重试
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
        系统设置
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">
        工作空间
      </h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">
        为不同业务或项目建立独立空间；模型渠道共享，知识、笔记、分类和回收站各自隔离。
      </p>
      <SettingsSectionNav active="workspaces" />

      <section className="mt-6 rounded-2xl border border-blue-200 bg-blue-50/50 p-5 text-sm leading-6 text-blue-900/80">
        <h2 className="text-sm font-semibold text-blue-900">关于工作空间</h2>
        <p className="mt-2">
          所有工作空间共用同一套对话模型和向量模型（在「对话模型」与「向量与索引」中配置）。
          每个工作空间的分类、文档、笔记、回收站和资料计数相互独立，切换空间后看到的资料也会随之切换。
        </p>
        <p className="mt-2">
          「默认空间」始终存在且不可归档；其它工作空间可以随时归档，归档后其中的资料会保留，但不再出现在活跃空间列表中，需要时可在此页面还原。
        </p>
      </section>

      {error && (
        <p className="mt-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </p>
      )}
      {message && (
        <p className="mt-5 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          {message}
        </p>
      )}

      <section className="mt-6 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <header>
          <h2 className="text-base font-semibold text-slate-900">新建工作空间</h2>
          <p className="mt-1 text-xs text-slate-500">
            名称用于在导航和资料中展示；标识（slug）作为内部代号，会出现在 URL 与接口中，建议创建后保持稳定。
          </p>
        </header>
        <form className="mt-4 space-y-4" onSubmit={handleCreate}>
          <FormField
            id="workspace-name"
            label="名称"
            required
            help="例：研究资料、家庭账本"
          >
            <input
              id="workspace-name"
              name="workspace-name"
              type="text"
              required
              maxLength={255}
              value={createForm.name}
              onChange={(event) => handleCreateNameChange(event.target.value)}
              disabled={creating}
              className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-slate-400 disabled:bg-slate-50"
            />
          </FormField>
          <FormField
            id="workspace-slug"
            label="标识（slug）"
            required
            help="小写字母、数字或连字符；以字母或数字开头；最长 64 个字符；创建后建议不要修改。"
          >
            <input
              id="workspace-slug"
              name="workspace-slug"
              type="text"
              required
              maxLength={64}
              value={createForm.slug}
              onChange={(event) => handleCreateSlugChange(event.target.value)}
              disabled={creating}
              pattern="[a-z0-9][a-z0-9\-]*"
              autoComplete="off"
              className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:border-slate-400 disabled:bg-slate-50"
            />
          </FormField>
          <FormField id="workspace-description" label="描述（可选）">
            <textarea
              id="workspace-description"
              name="workspace-description"
              rows={2}
              maxLength={2000}
              value={createForm.description}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  description: event.target.value,
                }))
              }
              disabled={creating}
              placeholder="说明此空间的用途，例如：「放置家庭账本与日常清单」"
              className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-slate-400 disabled:bg-slate-50"
            />
          </FormField>
          {createError && (
            <p className="text-sm text-red-700">{createError}</p>
          )}
          <div className="flex items-center justify-end">
            <button
              type="submit"
              disabled={creating || inFlight}
              className="rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
            >
              {creating ? '正在创建…' : '创建工作空间'}
            </button>
          </div>
        </form>
      </section>

      {workspaces === null ? (
        <p className="mt-6 text-sm text-slate-500">工作空间加载中…</p>
      ) : (
        <>
          <section className="mt-8">
            <header className="flex items-end justify-between gap-3">
              <div>
                <h2 className="text-base font-semibold text-slate-900">
                  活跃工作空间
                </h2>
                <p className="mt-1 text-xs text-slate-500">
                  {active.length} 个空间可用于日常访问。
                </p>
              </div>
            </header>
            {active.length === 0 ? (
              <p className="mt-3 rounded-xl border border-dashed border-slate-200 p-8 text-center text-sm text-slate-500">
                还没有活跃的工作空间。
              </p>
            ) : (
              <ul className="mt-3 space-y-3">
                {active.map((workspace) => (
                  <li key={workspace.slug}>
                    <WorkspaceCard
                      workspace={workspace}
                      isCurrent={current?.slug === workspace.slug}
                      isEditing={editingSlug === workspace.slug}
                      editForm={editForm}
                      onEditFormChange={setEditForm}
                      savingEdit={savingEdit}
                      archiving={archivingSlug === workspace.slug}
                      restoring={restoringSlug === workspace.slug}
                      switching={switchingSlug === workspace.slug}
                      inFlight={inFlight}
                      onStartEdit={startEdit}
                      onCancelEdit={cancelEdit}
                      onSaveEdit={(event) => void saveEdit(event, workspace)}
                      onArchive={() => requestArchive(workspace)}
                      onSwitch={() => switchTo(workspace)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </section>

          {archived.length > 0 && (
            <section className="mt-8">
              <header>
                <h2 className="text-base font-semibold text-slate-900">
                  已归档
                </h2>
                <p className="mt-1 text-xs text-slate-500">
                  归档空间中的资料仍保留，需要时可还原回活跃列表。
                </p>
              </header>
              <ul className="mt-3 space-y-3">
                {archived.map((workspace) => (
                  <li key={workspace.slug}>
                    <WorkspaceCard
                      workspace={workspace}
                      isCurrent={current?.slug === workspace.slug}
                      isEditing={editingSlug === workspace.slug}
                      editForm={editForm}
                      onEditFormChange={setEditForm}
                      savingEdit={savingEdit}
                      archiving={archivingSlug === workspace.slug}
                      restoring={restoringSlug === workspace.slug}
                      switching={switchingSlug === workspace.slug}
                      inFlight={inFlight}
                      onStartEdit={startEdit}
                      onCancelEdit={cancelEdit}
                      onSaveEdit={(event) => void saveEdit(event, workspace)}
                      onRestore={() => void restore(workspace)}
                    />
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </main>
  );
}

function WorkspaceCard({
  workspace,
  isCurrent,
  isEditing,
  editForm,
  onEditFormChange,
  savingEdit,
  archiving,
  restoring,
  switching,
  inFlight,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onArchive,
  onRestore,
  onSwitch,
}: {
  workspace: Workspace;
  isCurrent: boolean;
  isEditing: boolean;
  editForm: EditForm;
  onEditFormChange: (next: EditForm) => void;
  savingEdit: boolean;
  archiving: boolean;
  restoring: boolean;
  switching: boolean;
  inFlight: boolean;
  onStartEdit: (workspace: Workspace) => void;
  onCancelEdit: () => void;
  onSaveEdit: (event: FormEvent) => void;
  onArchive?: () => void;
  onRestore?: () => void;
  onSwitch?: () => void;
}) {
  const isArchived = workspace.status === 'archived';
  const isDefault = workspace.is_default;
  const canSwitch = !isArchived && !isCurrent;
  const canArchive = !isArchived && !isDefault;
  const canRestore = isArchived;

  if (isEditing) {
    return (
      <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <form className="space-y-4" onSubmit={onSaveEdit}>
          <FormField id={`edit-name-${workspace.slug}`} label="名称" required>
            <input
              id={`edit-name-${workspace.slug}`}
              type="text"
              required
              maxLength={255}
              value={editForm.name}
              onChange={(event) =>
                onEditFormChange({ ...editForm, name: event.target.value })
              }
              disabled={savingEdit}
              className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-slate-400 disabled:bg-slate-50"
            />
          </FormField>
          <FormField id={`edit-description-${workspace.slug}`} label="描述（可选）">
            <textarea
              id={`edit-description-${workspace.slug}`}
              rows={2}
              maxLength={2000}
              value={editForm.description}
              onChange={(event) =>
                onEditFormChange({
                  ...editForm,
                  description: event.target.value,
                })
              }
              disabled={savingEdit}
              className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-slate-400 disabled:bg-slate-50"
            />
          </FormField>
          <div className="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              onClick={onCancelEdit}
              disabled={savingEdit}
              className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={savingEdit || inFlight}
              className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
            >
              {savingEdit ? '保存中…' : '保存修改'}
            </button>
          </div>
        </form>
      </article>
    );
  }

  return (
    <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-lg font-semibold text-slate-900">
            {workspace.name}
          </h3>
          <p className="mt-1 flex items-center gap-2 text-xs text-slate-400">
            <span>标识</span>
            <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
              {workspace.slug}
            </code>
          </p>
          {workspace.description ? (
            <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
              {workspace.description}
            </p>
          ) : (
            <p className="mt-2 text-sm text-slate-400">（暂无描述）</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {isDefault ? (
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">
              默认空间
            </span>
          ) : null}
          {isCurrent ? (
            <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs text-emerald-700">
              当前
            </span>
          ) : null}
          {isArchived ? (
            <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs text-amber-700">
              已归档
            </span>
          ) : null}
        </div>
      </header>
      <div className="mt-4 flex flex-wrap gap-2">
        {canSwitch ? (
          <button
            type="button"
            onClick={onSwitch}
            disabled={inFlight}
            className="rounded-xl bg-slate-950 px-3.5 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
          >
            {switching ? '正在切换…' : '切换到此空间'}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => onStartEdit(workspace)}
          disabled={inFlight}
          className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        >
          编辑
        </button>
        {canArchive ? (
          <button
            type="button"
            onClick={onArchive}
            disabled={inFlight}
            className="rounded-xl border border-red-200 bg-white px-3.5 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-40"
          >
            {archiving ? '正在归档…' : '归档'}
          </button>
        ) : null}
        {canRestore ? (
          <button
            type="button"
            onClick={onRestore}
            disabled={inFlight}
            className="rounded-xl border border-emerald-200 bg-white px-3.5 py-2 text-sm text-emerald-700 hover:bg-emerald-50 disabled:opacity-40"
          >
            {restoring ? '正在还原…' : '还原'}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function FormField({
  id,
  label,
  required = false,
  help,
  children,
}: {
  id: string;
  label: string;
  required?: boolean;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
        {required ? <span className="ml-1 text-red-600">*</span> : null}
      </label>
      {children}
      {help ? <p className="mt-1 text-xs text-slate-500">{help}</p> : null}
    </div>
  );
}

function deriveSlug(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

async function fetchJson<T>(url: string, fallback: string): Promise<T> {
  const response = await fetch(url, {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await readError(response, fallback));
  }
  return parseJson<T>(response);
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
    return (await parseJson<T>(response)) as T;
  } catch {
    return null;
  }
}

async function postJson<T>(url: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await readError(response, '请求失败'));
  }
  return parseJson<T>(response);
}

async function patchJson<T>(url: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(url, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await readError(response, '请求失败'));
  }
  return parseJson<T>(response);
}

async function parseJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!text) {
    throw new Error('服务器返回空响应');
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error('服务器返回的内容不是 JSON');
  }
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await parseJson<{ detail?: unknown }>(response);
    const detail = payload?.detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string' && message) return message;
    }
    if (typeof detail === 'string' && detail) return detail;
  } catch {
    // fall through
  }
  return fallback;
}

function setWorkspaceCookie(slug: string) {
  if (typeof document === 'undefined') return;
  const encoded = encodeURIComponent(slug);
  document.cookie = `cangzhi_workspace=${encoded}; Path=/; Max-Age=31536000; SameSite=Lax`;
}
