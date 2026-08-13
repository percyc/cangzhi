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

type IntegrationInfo = {
  base_url: string;
  mcp_url: string;
  proxy_mcp_url: string;
  authorization_header: string;
  workspace_slug: string;
};

const scopeLabels: Record<string, string> = {
  'knowledge:read': '读取文档',
  'knowledge:search': '检索知识',
  'knowledge:ask': '问知识库与表格精确计算',
};

export default function AccessSettingsPage() {
  const [items, setItems] = useState<AccessToken[]>([]);
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState([
    'knowledge:read',
    'knowledge:search',
  ]);
  const [plaintext, setPlaintext] = useState<string | null>(null);
  const [integration, setIntegration] = useState<IntegrationInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [currentWorkspaceSlug, setCurrentWorkspaceSlug] = useState('default');

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch('/api/access-tokens', { cache: 'no-store' }),
      fetch('/api/workspaces/current', { cache: 'no-store' }),
    ])
      .then(async ([tokenResponse, workspaceResponse]) => {
        if (!tokenResponse.ok) throw new Error(await errorMessage(tokenResponse));
        if (!workspaceResponse.ok)
          throw new Error(await errorMessage(workspaceResponse));
        return {
          tokens: (await tokenResponse.json()) as TokenList,
          workspace: (await workspaceResponse.json()) as { slug: string },
        };
      })
      .then((body) => {
        if (cancelled) return;
        setItems(body.tokens.items);
        setCurrentWorkspaceSlug(body.workspace.slug);
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
        integration: {
          api_path: string;
          mcp_path: string;
          authorization_header: string;
        };
      };
      setPlaintext(body.token);
      const publicBaseUrl = window.location.origin;
      const machineApiBaseUrl = defaultMachineApiBaseUrl(publicBaseUrl);
      setIntegration({
        base_url: machineApiBaseUrl,
        mcp_url: `${machineApiBaseUrl}${body.integration.mcp_path}`,
        proxy_mcp_url: `${publicBaseUrl}${body.integration.mcp_path}`,
        authorization_header: body.integration.authorization_header,
        workspace_slug: currentWorkspaceSlug,
      });
      setItems((current) => [body.item, ...current]);
      setName('');
      setCopiedKey(null);
      setCopyError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建令牌失败');
    } finally {
      setSaving(false);
    }
  };

  const deleteToken = async (item: AccessToken) => {
    if (!item.revoked_at) {
      setError('有效令牌不能直接删除，请先撤销');
      return;
    }
    if (
      !window.confirm(
        `永久删除“${item.name}”的令牌记录吗？删除后不可恢复。`,
      )
    ) {
      return;
    }
    try {
      const response = await fetch(`/api/access-tokens/${item.id}`, {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      setItems((current) => current.filter((value) => value.id !== item.id));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除令牌失败');
    }
  };

  const copy = async (key: string, value: string) => {
    try {
      await copyText(value);
      setCopiedKey(key);
      setCopyError(null);
      window.setTimeout(() => {
        setCopiedKey((current) => (current === key ? null : current));
      }, 1800);
    } catch {
      setCopyError('浏览器阻止了自动复制，请长按或选中文本手动复制。');
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
        让 Hermes、OpenClaw、CLI 或其他 Agent 将藏知作为个人可控的 AI 知识中枢。
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
                      <div className="flex items-center gap-2">
                        <span className="rounded-full bg-slate-100 px-2 py-1 text-[11px] text-slate-500">
                          已撤销
                        </span>
                        <button
                          type="button"
                          onClick={() => void deleteToken(item)}
                          className="text-xs text-red-600 hover:underline"
                        >
                          删除
                        </button>
                      </div>
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
                  {!item.revoked_at &&
                    !item.scopes.includes('knowledge:ask') && (
                      <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-800">
                        这枚令牌只能读取和检索，不能调用 MCP 的
                        knowledge_ask。若需要完整问答或表格精确计算，请新建令牌并勾选
                        “问知识库与表格精确计算”。
                      </p>
                    )}
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
          环境变量；通过 <code>CANGZHI_WORKSPACE</code> 选择工作空间，未设置时使用
          <code className="ml-1">default</code>。完整模板位于项目的
          <code className="ml-1">integrations/skills/cangzhi-knowledge</code>。
          支持 MCP 进度通知的平台调用 <code>knowledge_ask</code> 时，会实时显示检索、
          分析和回答生成阶段；其他平台仍使用普通 JSON 结果，不受影响。
        </p>
      </section>

      {plaintext && integration && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm"
        >
          <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl bg-white p-6 shadow-2xl">
            <p className="text-xs font-semibold uppercase tracking-[0.15em] text-amber-700">
              仅显示一次
            </p>
            <h2 className="mt-2 text-xl font-semibold text-slate-950">
              立即保存访问令牌
            </h2>
            <p className="mt-2 text-sm leading-6 text-slate-500">
              关闭后无法再次查看。如遗失，请撤销并重新创建。
            </p>
            <CopyBlock
              className="mt-4"
              label="访问令牌"
              value={plaintext}
              copied={copiedKey === 'token'}
              onCopy={() => void copy('token', plaintext)}
              tone="dark"
            />

            <div className="mt-6 rounded-2xl border border-blue-100 bg-blue-50/60 p-4">
              <h3 className="text-sm font-semibold text-slate-900">
                远程 MCP 配置
              </h3>
              <p className="mt-1 text-xs leading-5 text-slate-500">
                在 Hermes、OpenClaw 或其他支持远程 MCP 的平台中填写以下地址和请求头。
              </p>
              {!scopes.includes('knowledge:ask') && (
                <p className="mt-3 rounded-lg bg-amber-100/70 px-3 py-2 text-xs leading-5 text-amber-900">
                  当前令牌未包含 knowledge:ask，只能使用 knowledge_search 获取片段，
                  不能让藏知直接回答或精确计算表格。
                </p>
              )}
              <CopyBlock
                className="mt-4"
                label="MCP URL（API 直连，Dify 等外部平台推荐）"
                value={integration.mcp_url}
                copied={copiedKey === 'mcp-url'}
                onCopy={() => void copy('mcp-url', integration.mcp_url)}
              />
              {integration.proxy_mcp_url !== integration.mcp_url && (
                <CopyBlock
                  className="mt-3"
                  label="MCP URL（同源代理，直连端口不可访问时使用）"
                  value={integration.proxy_mcp_url}
                  copied={copiedKey === 'proxy-mcp-url'}
                  onCopy={() =>
                    void copy('proxy-mcp-url', integration.proxy_mcp_url)
                  }
                />
              )}
              <CopyBlock
                className="mt-3"
                label="认证请求头"
                value={`Authorization: ${integration.authorization_header}`}
                copied={copiedKey === 'auth-header'}
                onCopy={() =>
                  void copy(
                    'auth-header',
                    `Authorization: ${integration.authorization_header}`,
                  )
                }
              />
              <CopyBlock
                className="mt-3"
                label="工作空间请求头（按需替换 slug）"
                value={`X-Cangzhi-Workspace: ${integration.workspace_slug}`}
                copied={copiedKey === 'workspace-header'}
                onCopy={() =>
                  void copy(
                    'workspace-header',
                    `X-Cangzhi-Workspace: ${integration.workspace_slug}`,
                  )
                }
              />
              <CopyBlock
                className="mt-3"
                label="通用 JSON 配置"
                value={mcpConfig(integration)}
                copied={copiedKey === 'mcp-json'}
                onCopy={() => void copy('mcp-json', mcpConfig(integration))}
                multiline
              />
              <p className="mt-2 text-[11px] leading-5 text-slate-400">
                不同平台的顶层字段名称可能略有差异，但 URL、传输方式和请求头保持一致。
              </p>
            </div>

            <div className="mt-4 rounded-2xl border border-slate-200 p-4">
              <h3 className="text-sm font-semibold text-slate-900">
                CLI / Skill 环境变量
              </h3>
              <CopyBlock
                className="mt-3"
                label="Shell 配置"
                value={shellConfig(integration, plaintext)}
                copied={copiedKey === 'shell'}
                onCopy={() =>
                  void copy('shell', shellConfig(integration, plaintext))
                }
                multiline
              />
            </div>

            {copyError && (
              <p className="mt-4 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-800">
                {copyError}
              </p>
            )}

            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={() => {
                  setPlaintext(null);
                  setIntegration(null);
                  setCopiedKey(null);
                  setCopyError(null);
                }}
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

function CopyBlock({
  label,
  value,
  copied,
  onCopy,
  multiline = false,
  tone = 'light',
  className = '',
}: {
  label: string;
  value: string;
  copied: boolean;
  onCopy: () => void;
  multiline?: boolean;
  tone?: 'light' | 'dark';
  className?: string;
}) {
  const dark = tone === 'dark';
  return (
    <div className={className}>
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <span
          className={`text-xs font-medium ${
            dark ? 'text-slate-600' : 'text-slate-500'
          }`}
        >
          {label}
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
        >
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre
        className={`select-all overflow-x-auto rounded-xl p-3 text-xs leading-5 ${
          multiline ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'
        } ${
          dark
            ? 'bg-slate-950 text-emerald-300'
            : 'border border-slate-200 bg-white text-slate-700'
        }`}
      >
        {value}
      </pre>
    </div>
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

async function copyText(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!copied) throw new Error('copy failed');
}

function defaultMachineApiBaseUrl(publicBaseUrl: string) {
  const url = new URL(publicBaseUrl);
  // The bundled self-hosted deployment exposes Next.js on 3000 and the API on
  // 8000. Machine-to-machine streaming should bypass the generic Next rewrite.
  // Reverse-proxy/HTTPS deployments keep their public same-origin endpoint.
  if (url.protocol === 'http:' && url.port === '3000') {
    url.port = '8000';
  }
  return url.origin;
}

function mcpConfig(integration: IntegrationInfo) {
  return JSON.stringify(
    {
      mcpServers: {
        cangzhi: {
          type: 'streamable-http',
          url: integration.mcp_url,
          headers: {
            Authorization: integration.authorization_header,
            'X-Cangzhi-Workspace': integration.workspace_slug,
          },
        },
      },
    },
    null,
    2,
  );
}

function shellConfig(integration: IntegrationInfo, token: string) {
  return [
    `export CANGZHI_URL='${integration.base_url}'`,
    `export CANGZHI_TOKEN='${token}'`,
    `export CANGZHI_WORKSPACE='${integration.workspace_slug}'`,
  ].join('\n');
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
