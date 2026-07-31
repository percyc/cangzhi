'use client';

import { useEffect, useState } from 'react';

import { SettingsSectionNav } from '@/components/SettingsSectionNav';

type AccessToken = {
  id: number;
  name: string;
  token_prefix: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

type TokenList = {
  items: AccessToken[];
  available_scopes: string[];
};

const scopeLabels: Record<string, string> = {
  'knowledge:read': '读取文档',
  'knowledge:search': '检索知识',
  'knowledge:ask': '调用藏知问答模型',
};

export default function AccessSettingsPage() {
  const [items, setItems] = useState<AccessToken[]>([]);
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState([
    'knowledge:read',
    'knowledge:search',
  ]);
  const [plaintext, setPlaintext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/access-tokens', { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return (await response.json()) as TokenList;
      })
      .then((body) => {
        if (cancelled) return;
        setItems(body.items);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : '读取令牌失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const createToken = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || scopes.length === 0 || saving) return;
    setSaving(true);
    setError(null);
    try {
      const response = await fetch('/api/access-tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), scopes }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const body = (await response.json()) as {
        token: string;
        item: AccessToken;
      };
      setPlaintext(body.token);
      setItems((current) => [body.item, ...current]);
      setName('');
      setCopied(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建令牌失败');
    } finally {
      setSaving(false);
    }
  };

  const revoke = async (item: AccessToken) => {
    if (!window.confirm(`撤销“${item.name}”吗？对应客户端会立即无法访问。`)) {
      return;
    }
    try {
      const response = await fetch(`/api/access-tokens/${item.id}/revoke`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const body = (await response.json()) as { item: AccessToken };
      setItems((current) =>
        current.map((value) => (value.id === item.id ? body.item : value)),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '撤销令牌失败');
    }
  };

  return (
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
        系统设置
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">
        外部接入
      </h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">
        让 Hermes、OpenClaw、CLI 或其他 Agent 将藏知作为个人知识中枢。
        每个客户端使用独立令牌，可以随时撤销。
      </p>

      <SettingsSectionNav active="access" />

      {error && (
        <p className="mt-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </p>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="text-base font-semibold text-slate-900">创建访问令牌</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            建议默认只允许读取和检索。只有需要由藏知模型直接回答时，才开放问答权限。
          </p>
          <form className="mt-5 space-y-5" onSubmit={createToken}>
            <label className="block">
              <span className="text-sm font-medium text-slate-700">名称</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：Hermes 笔记助手"
                maxLength={255}
                className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-slate-400"
              />
            </label>
            <fieldset>
              <legend className="text-sm font-medium text-slate-700">权限</legend>
              <div className="mt-2 space-y-2">
                {Object.entries(scopeLabels).map(([value, label]) => (
                  <label
                    key={value}
                    className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-100 p-3 hover:bg-slate-50"
                  >
                    <input
                      type="checkbox"
                      checked={scopes.includes(value)}
                      onChange={() =>
                        setScopes((current) =>
                          current.includes(value)
                            ? current.filter((item) => item !== value)
                            : [...current, value],
                        )
                      }
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block text-sm text-slate-700">{label}</span>
                      <span className="mt-0.5 block font-mono text-[11px] text-slate-400">
                        {value}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
            <button
              type="submit"
              disabled={!name.trim() || scopes.length === 0 || saving}
              className="w-full rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
            >
              {saving ? '正在创建…' : '创建令牌'}
            </button>
          </form>
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-slate-900">已有令牌</h2>
              <p className="mt-1 text-xs text-slate-500">
                这里只显示安全前缀，不保存完整令牌。
              </p>
            </div>
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-500">
              {items.filter((item) => !item.revoked_at).length} 个有效
            </span>
          </div>
          {loading ? (
            <p className="mt-6 text-sm text-slate-500">加载中…</p>
          ) : items.length === 0 ? (
            <div className="mt-6 rounded-xl border border-dashed border-slate-200 p-8 text-center text-sm text-slate-500">
              还没有外部客户端令牌
            </div>
          ) : (
            <ul className="mt-4 divide-y divide-slate-100">
              {items.map((item) => (
                <li key={item.id} className="py-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-slate-800">
                        {item.name}
                      </p>
                      <p className="mt-1 font-mono text-xs text-slate-400">
                        {item.token_prefix}…
                      </p>
                    </div>
                    {item.revoked_at ? (
                      <span className="rounded-full bg-slate-100 px-2 py-1 text-[11px] text-slate-500">
                        已撤销
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => void revoke(item)}
                        className="text-xs text-red-600 hover:underline"
                      >
                        撤销
                      </button>
                    )}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {item.scopes.map((scope) => (
                      <span
                        key={scope}
                        className="rounded-full bg-slate-50 px-2 py-1 text-[11px] text-slate-500"
                      >
                        {scopeLabels[scope] ?? scope}
                      </span>
                    ))}
                  </div>
                  <p className="mt-2 text-[11px] text-slate-400">
                    创建于 {formatTime(item.created_at)}
                    {item.last_used_at
                      ? ` · 最近使用 ${formatTime(item.last_used_at)}`
                      : ' · 尚未使用'}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="mt-6 rounded-2xl border border-slate-200 bg-slate-50 p-5">
        <h2 className="text-sm font-semibold text-slate-800">接入地址</h2>
        <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
          <Endpoint label="REST API" value="/api/v1" />
          <Endpoint label="MCP Streamable HTTP" value="/api/mcp" />
        </dl>
        <p className="mt-4 text-xs leading-5 text-slate-500">
          CLI 使用 <code>CANGZHI_URL</code> 和 <code>CANGZHI_TOKEN</code>
          环境变量。完整模板位于项目的
          <code className="ml-1">integrations/skills/cangzhi-knowledge</code>。
        </p>
      </section>

      {plaintext && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm"
        >
          <div className="w-full max-w-xl rounded-2xl bg-white p-6 shadow-2xl">
            <p className="text-xs font-semibold uppercase tracking-[0.15em] text-amber-700">
              仅显示一次
            </p>
            <h2 className="mt-2 text-xl font-semibold text-slate-950">
              立即保存访问令牌
            </h2>
            <p className="mt-2 text-sm leading-6 text-slate-500">
              关闭后无法再次查看。如遗失，请撤销并重新创建。
            </p>
            <pre className="mt-4 overflow-x-auto rounded-xl bg-slate-950 p-4 text-xs leading-6 text-emerald-300">
              {plaintext}
            </pre>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={async () => {
                  await navigator.clipboard.writeText(plaintext);
                  setCopied(true);
                }}
                className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50"
              >
                {copied ? '已复制' : '复制令牌'}
              </button>
              <button
                type="button"
                onClick={() => setPlaintext(null)}
                className="rounded-xl bg-slate-950 px-4 py-2 text-sm text-white"
              >
                我已保存
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

function Endpoint({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <dt className="text-slate-400">{label}</dt>
      <dd className="mt-1 font-mono text-slate-700">{value}</dd>
    </div>
  );
}

async function errorMessage(response: Response) {
  const body = await response.json().catch(() => ({}));
  return body?.detail?.message ?? '请求失败';
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}
