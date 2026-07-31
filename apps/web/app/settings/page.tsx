'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import {
  fetchAIConfig,
  fetchAIModels,
  fetchEmbeddingModels,
  fetchEmbeddingStatus,
  runEmbeddingProfileAction,
  testAIConfig,
  testEmbeddingCompatibility,
  updateAIConfig,
  type AIConfig,
  type AIModelsResponse,
  type AITestResult,
  type EmbeddingCompatibilityResult,
  type EmbeddingStatus,
} from '@/lib/api';
import {
  SettingsSectionNav,
  type SettingsSection,
} from '@/components/SettingsSectionNav';

type Provider = AIConfig['provider'];
type EmbeddingProvider = AIConfig['embedding_provider'];
type SaveState = 'idle' | 'saving' | 'saved' | 'error';
type FetchState = 'idle' | 'fetching' | 'success' | 'error';
type SettingsPanel = Extract<SettingsSection, 'chat' | 'embedding'>;

export default function SettingsPage() {
  const router = useRouter();
  const [config, setConfig] = useState<AIConfig | null>(null);
  const [activePanel, setActivePanel] = useState<SettingsPanel>('chat');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [provider, setProvider] = useState<Provider>('disabled');
  const [openaiBaseUrl, setOpenaiBaseUrl] = useState('');
  const [openaiModel, setOpenaiModel] = useState('');
  const [ollamaBaseUrl, setOllamaBaseUrl] = useState('http://localhost:11434');
  const [ollamaModel, setOllamaModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [timeoutSeconds, setTimeoutSeconds] = useState(30);
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<AITestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [models, setModels] = useState<AIModelsResponse['models']>([]);
  const [modelsFetchState, setModelsFetchState] = useState<FetchState>('idle');
  const [modelsFetchError, setModelsFetchError] = useState<string | null>(null);
  // --- Independent embedding channel (ADR-015 phase 1) ---
  const [embeddingProvider, setEmbeddingProvider] =
    useState<EmbeddingProvider>('disabled');
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState('');
  const [embeddingModel, setEmbeddingModel] = useState('');
  const [embeddingApiKey, setEmbeddingApiKey] = useState('');
  const [clearEmbeddingApiKey, setClearEmbeddingApiKey] = useState(false);
  const [embeddingTimeoutSeconds, setEmbeddingTimeoutSeconds] = useState(30);
  const [embeddingTestResult, setEmbeddingTestResult] =
    useState<EmbeddingCompatibilityResult | null>(null);
  const [embeddingStatus, setEmbeddingStatus] =
    useState<EmbeddingStatus | null>(null);
  const [embeddingActionId, setEmbeddingActionId] = useState<number | null>(null);
  const [embeddingActionMessage, setEmbeddingActionMessage] =
    useState<string | null>(null);
  const [embeddingTesting, setEmbeddingTesting] = useState(false);
  const [embeddingModels, setEmbeddingModels] =
    useState<AIModelsResponse['models']>([]);
  const [embeddingModelsState, setEmbeddingModelsState] =
    useState<FetchState>('idle');
  const [embeddingModelsError, setEmbeddingModelsError] =
    useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAIConfig()
      .then((data) => {
        if (cancelled) return;
        setConfig(data);
        setProvider(data.provider);
        setOpenaiBaseUrl(data.openai_base_url ?? 'https://api.openai.com/v1');
        setOpenaiModel(data.openai_model ?? '');
        setOllamaBaseUrl(data.ollama_base_url ?? 'http://localhost:11434');
        setOllamaModel(data.ollama_model ?? '');
        setTimeoutSeconds(data.timeout_seconds || 30);
        setEmbeddingProvider(data.embedding_provider);
        setEmbeddingBaseUrl(
          data.embedding_base_url ?? 'https://api.openai.com/v1',
        );
        setEmbeddingModel(data.embedding_model ?? '');
        setEmbeddingTimeoutSeconds(data.embedding_timeout_seconds || 30);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : '读取设置失败');
      });
    fetchEmbeddingStatus()
      .then((data) => {
        if (!cancelled) setEmbeddingStatus(data);
      })
      .catch(() => {
        // 兼容状态不应阻断基础设置页；测试时仍会显示具体错误。
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const syncPanelFromHash = () => {
      setActivePanel(window.location.hash === '#embedding' ? 'embedding' : 'chat');
    };
    syncPanelFromHash();
    window.addEventListener('hashchange', syncPanelFromHash);
    return () => window.removeEventListener('hashchange', syncPanelFromHash);
  }, []);

  useEffect(() => {
    if (!embeddingStatus?.profiles.some((profile) => profile.status === 'building')) {
      return;
    }
    const timer = window.setInterval(() => {
      fetchEmbeddingStatus()
        .then(setEmbeddingStatus)
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [embeddingStatus]);

  const invalidateModels = () => {
    setModels([]);
    setModelsFetchState('idle');
    setModelsFetchError(null);
  };

  const connectionChanged =
    provider !== config?.provider ||
    (provider === 'openai' &&
      (openaiBaseUrl.trim() !== (config?.openai_base_url ?? '') ||
        Boolean(apiKey) ||
        clearKey)) ||
    (provider === 'ollama' &&
      ollamaBaseUrl.trim() !== (config?.ollama_base_url ?? ''));
  const modelChanged =
    (provider === 'openai' &&
      openaiModel.trim() !== (config?.openai_model ?? '')) ||
    (provider === 'ollama' &&
      ollamaModel.trim() !== (config?.ollama_model ?? ''));
  const chatDirty =
    connectionChanged ||
    modelChanged ||
    timeoutSeconds !== (config?.timeout_seconds || 30);

  // The embedding channel has its own change tracking: changing
  // the base URL, model, key or timeout only invalidates the
  // embedding-side test, never the chat-side one.
  const embeddingConnectionChanged =
    embeddingProvider !== config?.embedding_provider ||
    (embeddingProvider !== 'disabled' &&
      embeddingBaseUrl.trim() !== (config?.embedding_base_url ?? '')) ||
    Boolean(embeddingApiKey) ||
    clearEmbeddingApiKey;
  const embeddingModelChanged =
    embeddingModel.trim() !== (config?.embedding_model ?? '');
  const embeddingTimeoutChanged =
    embeddingTimeoutSeconds !== (config?.embedding_timeout_seconds || 30);
  const embeddingDirty =
    embeddingConnectionChanged ||
    embeddingModelChanged ||
    embeddingTimeoutChanged;
  const dirtyPanelCount = Number(chatDirty) + Number(embeddingDirty);
  const anyDirty = dirtyPanelCount > 0;

  const handleFetchModels = async () => {
    if (connectionChanged) {
      setModelsFetchState('error');
      setModelsFetchError('地址、密钥或模型来源有改动，请先保存设置');
      return;
    }
    setModelsFetchState('fetching');
    setModelsFetchError(null);
    try {
      const result = await fetchAIModels();
      setModels(result.models);
      setModelsFetchState('success');
    } catch (err) {
      setModelsFetchState('error');
      setModelsFetchError(
        err instanceof Error ? err.message : '获取模型列表失败',
      );
    }
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!config) return;
    setSaveState('saving');
    setSaveMessage(null);
    setTestResult(null);
    setEmbeddingTestResult(null);

    const payload: Parameters<typeof updateAIConfig>[0] = {
      provider,
      api_key_action: (clearKey ? 'clear' : apiKey ? 'replace' : 'keep') as
        'keep' | 'replace' | 'clear',
      api_key: clearKey ? undefined : apiKey || undefined,
      timeout_seconds: timeoutSeconds,
      openai:
        provider === 'openai'
          ? { base_url: openaiBaseUrl.trim(), model: openaiModel.trim() }
          : undefined,
      ollama:
        provider === 'ollama'
          ? { base_url: ollamaBaseUrl.trim(), model: ollamaModel.trim() }
          : undefined,
      embedding_model: embeddingModel.trim() || null,
    };
    if (embeddingProvider !== config.embedding_provider) {
      payload.embedding_provider = embeddingProvider;
    }
    if (embeddingProvider !== 'disabled') {
      payload.embedding_base_url = embeddingBaseUrl.trim();
    }
    if (
      embeddingApiKey ||
      clearEmbeddingApiKey ||
      embeddingProvider !== config.embedding_provider
    ) {
      payload.embedding_api_key_action = (
        clearEmbeddingApiKey ? 'clear' : embeddingApiKey ? 'replace' : 'keep'
      ) as 'keep' | 'replace' | 'clear';
      payload.embedding_api_key = clearEmbeddingApiKey
        ? undefined
        : embeddingApiKey || undefined;
    }
    if (embeddingTimeoutSeconds !== (config.embedding_timeout_seconds || 30)) {
      payload.embedding_timeout_seconds = embeddingTimeoutSeconds;
    }

    try {
      const next = await updateAIConfig(payload);
      setConfig(next);
      invalidateModels();
      setApiKey('');
      setClearKey(false);
      setEmbeddingApiKey('');
      setClearEmbeddingApiKey(false);
      setSaveState('saved');
      setSaveMessage('设置已保存');
      router.refresh();
    } catch (err) {
      setSaveState('error');
      setSaveMessage(err instanceof Error ? err.message : '保存失败');
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testAIConfig();
      setTestResult(result);
    } catch (err) {
      setTestResult({
        ok: false,
        message: err instanceof Error ? err.message : '测试连接失败',
      });
    } finally {
      setTesting(false);
    }
  };

  const handleEmbeddingTest = async () => {
    setEmbeddingTesting(true);
    setEmbeddingTestResult(null);
    try {
      const result = await testEmbeddingCompatibility();
      setEmbeddingTestResult(result);
      setEmbeddingStatus(await fetchEmbeddingStatus());
    } catch (err) {
      setEmbeddingTestResult({
        ok: false,
        decision: 'rebuild_required',
        reason: err instanceof Error ? err.message : '测试向量兼容性失败',
        profile: { id: null, status: null },
        scores: [],
      } as EmbeddingCompatibilityResult);
    } finally {
      setEmbeddingTesting(false);
    }
  };

  const handleFetchEmbeddingModels = async () => {
    if (
      embeddingConnectionChanged ||
      embeddingTimeoutChanged ||
      embeddingProvider === 'disabled'
    ) {
      setEmbeddingModelsState('error');
      setEmbeddingModelsError('请先保存向量渠道、地址、密钥和超时设置');
      return;
    }
    setEmbeddingModelsState('fetching');
    setEmbeddingModelsError(null);
    try {
      const result = await fetchEmbeddingModels();
      setEmbeddingModels(result.models);
      setEmbeddingModelsState('success');
    } catch (err) {
      setEmbeddingModelsState('error');
      setEmbeddingModelsError(
        err instanceof Error ? err.message : '获取 Embedding 模型列表失败',
      );
    }
  };

  const handleEmbeddingAction = async (
    profileId: number,
    action: 'build' | 'retry' | 'activate' | 'rollback' | 'delete',
  ) => {
    if (
      action === 'delete' &&
      !window.confirm('确定删除这个向量索引版本吗？对应向量数据将被永久清理。')
    ) return;
    setEmbeddingActionId(profileId);
    setEmbeddingActionMessage(null);
    try {
      const result = await runEmbeddingProfileAction(profileId, action);
      const labels = {
        build: `已开始构建，共 ${result.total_chunks ?? 0} 个切片`,
        retry: `已重新提交 ${result.enqueued ?? 0} 个失败任务`,
        activate: '新向量索引已启用',
        rollback: '已切回历史向量索引',
        delete: '向量索引版本已删除',
      };
      setEmbeddingActionMessage(labels[action]);
      setEmbeddingStatus(await fetchEmbeddingStatus());
    } catch (err) {
      setEmbeddingActionMessage(
        err instanceof Error ? err.message : '向量索引操作失败',
      );
    } finally {
      setEmbeddingActionId(null);
    }
  };

  if (loadError) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-12">
        <h1 className="text-2xl font-semibold text-slate-900">模型设置</h1>
        <p className="mt-4 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          读取设置失败：{loadError}
        </p>
      </main>
    );
  }

  if (!config) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-12">
        <p className="text-sm text-slate-500">设置加载中…</p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-5xl px-4 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">设置</h1>
      <p className="mt-2 text-sm text-slate-500">
        管理模型、知识来源和系统处理能力。日常整理请前往知识库或收件箱。
      </p>
      <SettingsSectionNav
        active={activePanel}
        hints={{
          chat:
            provider === 'disabled'
              ? '当前未启用'
              : currentChatModel(provider, openaiModel, ollamaModel),
          embedding: embeddingStatus?.active_profile.id
            ? `${embeddingStatus.active_profile.model} · ${embeddingStatus.active_profile.dim} 维`
            : '当前未启用',
        }}
        dirty={{ chat: chatDirty, embedding: embeddingDirty }}
        onSelect={(section) => {
          if (section === 'chat' || section === 'embedding') {
            setActivePanel(section);
          }
        }}
        beforeNavigate={(section) =>
          (section !== 'sources' && section !== 'data') ||
          !anyDirty ||
          window.confirm('模型设置还有未保存的修改，确定离开当前页面吗？')
        }
      />

      <form className="mt-6 space-y-6" onSubmit={handleSave}>
        {activePanel === 'chat' && (
        <section className="space-y-5 rounded-2xl border-2 border-blue-200 bg-white p-5 shadow-sm">
          <header className="border-b border-blue-100 pb-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-blue-600">
              对话与理解能力
            </p>
            <h2 className="mt-1 text-xl font-semibold text-slate-900">
              对话模型
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              用于自动分类、摘要、整理笔记和知识库问答。
            </p>
          </header>
          <div>
            <h3 className="text-sm font-semibold text-slate-800">
              1. 选择对话模型来源
            </h3>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              {(['disabled', 'openai', 'ollama'] as const).map((value) => {
              const labels: Record<Provider, { title: string; hint: string }> =
                {
                  disabled: {
                    title: '不使用',
                    hint: '资料会正常入库，但不会调用 AI',
                  },
                  openai: {
                    title: 'OpenAI 兼容',
                    hint: '支持 OpenAI 以及兼容协议的服务',
                  },
                  ollama: {
                    title: 'Ollama / 本地',
                    hint: '本地运行的 Ollama 服务',
                  },
                };
              const label = labels[value];
              const active = provider === value;
              const style = active
                ? 'border-slate-900 bg-slate-900 text-white'
                : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50';
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => {
                    setProvider(value);
                    invalidateModels();
                  }}
                  className={`rounded-xl border p-4 text-left ${style}`}
                >
                  <p className="text-sm font-semibold">{label.title}</p>
                  <p
                    className={`mt-1 text-xs ${active ? 'text-slate-200' : 'text-slate-500'}`}
                  >
                    {label.hint}
                  </p>
                </button>
              );
              })}
            </div>
          </div>

          {provider === 'openai' && (
            <section className="space-y-4 rounded-xl bg-slate-50 p-4">
              <h3 className="text-sm font-semibold text-slate-800">
                2. 填写对话模型参数
              </h3>
            <Field
              id="openai-base-url"
              label="API 地址"
              help="形如 https://api.openai.com/v1"
              value={openaiBaseUrl}
              onChange={(value) => {
                setOpenaiBaseUrl(value);
                invalidateModels();
              }}
            />
            <FieldWithList
              id="openai-model"
              label="模型名称"
              help="保存地址和密钥后，可获取模型列表快速选择；也可以手动输入。"
              value={openaiModel}
              onChange={setOpenaiModel}
              models={models}
            />
            <div>
              <label htmlFor="api-key" className="block text-sm text-slate-700">
                API 密钥
              </label>
              <input
                id="api-key"
                name="api-key"
                type="password"
                value={apiKey}
                onChange={(event) => {
                  setApiKey(event.target.value);
                  invalidateModels();
                  if (event.target.value) setClearKey(false);
                }}
                placeholder={
                  config.has_api_key ? '已保存（输入新值以替换）' : '请输入密钥'
                }
                autoComplete="off"
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
              />
              <p className="mt-1 text-xs text-slate-500">
                {config.has_api_key
                  ? '已保存一份密钥。输入新值会替换，留空则保持原密钥。'
                  : '尚未保存密钥。'}
              </p>
              <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={clearKey}
                  onChange={(event) => {
                    setClearKey(event.target.checked);
                    invalidateModels();
                    if (event.target.checked) setApiKey('');
                  }}
                />
                明确清除已保存的密钥
              </label>
            </div>
            </section>
          )}

          {provider === 'ollama' && (
            <section className="space-y-4 rounded-xl bg-slate-50 p-4">
              <h3 className="text-sm font-semibold text-slate-800">
                2. 填写对话模型参数
              </h3>
            <Field
              id="ollama-base-url"
              label="服务地址"
              help="形如 http://localhost:11434"
              value={ollamaBaseUrl}
              onChange={(value) => {
                setOllamaBaseUrl(value);
                invalidateModels();
              }}
            />
            <FieldWithList
              id="ollama-model"
              label="模型名称"
              help="保存服务地址后，可获取本地模型列表快速选择；也可以手动输入。"
              value={ollamaModel}
              onChange={setOllamaModel}
              models={models}
            />
            </section>
          )}

          {provider !== 'disabled' && (
            <section className="space-y-4 rounded-xl border border-blue-100 bg-blue-50/40 p-4">
            <div>
              <h2 className="text-base font-semibold text-slate-800">
                3. 获取模型与请求设置
              </h2>
              <p className="mt-1 text-xs text-slate-500">
                模型列表在这里获取；连接测试统一在页面底部执行。
              </p>
            </div>
            <Field
              id="timeout"
              label="对话模型请求超时（秒）"
              value={String(timeoutSeconds)}
              onChange={(value) => setTimeoutSeconds(Number(value) || 30)}
              type="number"
            />
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={handleFetchModels}
                disabled={modelsFetchState === 'fetching' || connectionChanged}
                className="rounded border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {modelsFetchState === 'fetching'
                  ? '正在获取对话模型…'
                  : models.length
                    ? '刷新对话模型列表'
                    : '获取对话模型列表'}
              </button>
            </div>
            {connectionChanged && (
              <p className="text-sm text-amber-700">
                请先保存对话模型的地址、密钥或来源改动
              </p>
            )}
            {modelsFetchError && (
              <p className="text-sm text-red-700">
                获取对话模型失败：{modelsFetchError}
              </p>
            )}
            {modelsFetchState === 'success' && (
              <p className="text-sm text-emerald-700">
                已获取 {models.length} 个对话模型，可在上方模型名称中选择
              </p>
            )}
            </section>
          )}
        </section>
        )}

        {activePanel === 'embedding' && (
        <section className="flex flex-col gap-4 rounded-2xl border-2 border-violet-200 bg-white p-5 shadow-sm">
          <header className="border-b border-violet-100 pb-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-violet-600">
              语义检索能力
            </p>
            <h2 className="mt-1 text-xl font-semibold text-slate-900">
              向量模型
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              仅用于语义检索和向量索引，不参与摘要或回答生成；关闭后仍可使用关键词检索。
            </p>
          </header>
          <div className="order-4 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
            <p className="mb-1 font-semibold text-slate-800">当前生效状态</p>
            {embeddingStatus?.active_profile.id
              ? `当前服务索引：${embeddingStatus.active_profile.model} · ${embeddingStatus.active_profile.dim} 维`
              : '当前尚未启用向量索引，检索继续使用关键词。'}
          </div>
          {embeddingStatus?.profiles?.length ? (
            <div className="order-5 space-y-2 rounded-lg border border-slate-200 p-3">
              <h3 className="text-sm font-medium text-slate-800">4. 向量索引版本</h3>
              {embeddingStatus.profiles.map((profile) => {
                const total = profile.total_chunks ?? 0;
                const completed = profile.completed_chunks ?? 0;
                const failed = profile.failed_chunks ?? 0;
                const progress = total ? Math.round((completed / total) * 100) : 0;
                const statusLabels: Record<string, string> = {
                  tested: '已测试，等待构建',
                  building: '正在构建',
                  ready: '构建完成，等待启用',
                  active: '当前使用',
                  retired: '历史版本',
                  failed: '构建失败',
                  draft: '草稿',
                };
                return (
                  <div key={profile.id} className="rounded border border-slate-200 bg-white p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium text-slate-800">
                          {profile.model} · {profile.dim} 维
                        </p>
                        <p className="text-xs text-slate-500">
                          {statusLabels[profile.status] ?? profile.status}
                          {total > 0 && ` · ${completed}/${total}（${progress}%）`}
                          {failed > 0 && ` · ${failed} 个失败`}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {profile.available_actions.map((action) => {
                          const labels = {
                            build: '构建索引',
                            retry: '重试失败任务',
                            activate: '启用此版本',
                            rollback: '回滚到此版本',
                            delete: '删除版本',
                          };
                          return (
                            <button
                              key={action}
                              type="button"
                              disabled={embeddingActionId !== null}
                              onClick={() => handleEmbeddingAction(profile.id, action)}
                              className={`rounded border px-3 py-1.5 text-xs disabled:opacity-50 ${
                                action === 'delete'
                                  ? 'border-red-200 text-red-700 hover:bg-red-50'
                                  : 'border-slate-300 text-slate-700 hover:bg-slate-50'
                              }`}
                            >
                              {embeddingActionId === profile.id ? '处理中…' : labels[action]}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                );
              })}
              {embeddingActionMessage && (
                <p className="text-sm text-slate-700">{embeddingActionMessage}</p>
              )}
            </div>
          ) : null}
          <div className="order-1">
            <h3 className="mb-3 text-sm font-semibold text-slate-800">
              1. 选择向量模型来源
            </h3>
            <div className="grid gap-3 sm:grid-cols-3">
              {(['disabled', 'openai', 'ollama'] as const).map((value) => {
              const labels: Record<
                EmbeddingProvider,
                { title: string; hint: string }
              > = {
                disabled: {
                  title: '不使用',
                  hint: '关闭 Embedding，仅使用关键词检索',
                },
                openai: {
                  title: 'OpenAI 兼容',
                  hint: '复用 OpenAI 协议，单独管理密钥',
                },
                ollama: {
                  title: 'Ollama / 本地',
                  hint: '本地 Ollama 服务',
                },
              };
              const label = labels[value];
              const active = embeddingProvider === value;
              const style = active
                ? 'border-slate-900 bg-slate-900 text-white'
                : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50';
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => setEmbeddingProvider(value)}
                  className={`rounded-xl border p-3 text-left ${style}`}
                >
                  <p className="text-sm font-semibold">{label.title}</p>
                  <p
                    className={`mt-1 text-xs ${
                      active ? 'text-slate-200' : 'text-slate-500'
                    }`}
                  >
                    {label.hint}
                  </p>
                </button>
              );
              })}
            </div>
          </div>

          {embeddingProvider !== 'disabled' && (
            <div className="order-2 space-y-4 rounded-xl bg-slate-50 p-4">
              <h3 className="text-sm font-semibold text-slate-800">
                2. 填写向量模型参数
              </h3>
              <Field
                id="embedding-base-url"
                label="Embedding 服务地址"
                help={
                  embeddingProvider === 'ollama'
                    ? '形如 http://localhost:11434'
                    : '形如 https://api.openai.com/v1'
                }
                value={embeddingBaseUrl}
                onChange={setEmbeddingBaseUrl}
              />
              {embeddingProvider === 'openai' && (
                <div>
                  <label
                    htmlFor="embedding-api-key"
                    className="block text-sm text-slate-700"
                  >
                    Embedding API 密钥
                  </label>
                  <input
                    id="embedding-api-key"
                    name="embedding-api-key"
                    type="password"
                    value={embeddingApiKey}
                    onChange={(event) => {
                      setEmbeddingApiKey(event.target.value);
                      if (event.target.value) setClearEmbeddingApiKey(false);
                    }}
                    placeholder={
                      config.has_embedding_api_key
                        ? '已保存（输入新值以替换）'
                        : '请输入密钥'
                    }
                    autoComplete="off"
                    className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
                  />
                  <p className="mt-1 text-xs text-slate-500">
                    {config.has_embedding_api_key
                      ? '已保存一份 Embedding 密钥。输入新值会替换，留空则保持原密钥。'
                      : '尚未保存 Embedding 密钥。'}
                  </p>
                  <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-600">
                    <input
                      type="checkbox"
                      checked={clearEmbeddingApiKey}
                      onChange={(event) => {
                        setClearEmbeddingApiKey(event.target.checked);
                        if (event.target.checked) setEmbeddingApiKey('');
                      }}
                    />
                    明确清除已保存的 Embedding 密钥
                  </label>
                </div>
              )}
              <FieldWithList
                id="embedding-model"
                label="Embedding 模型"
                help="可从上方获取到的模型列表中挑选，也支持手动输入。留空即关闭 Embedding。"
                value={embeddingModel}
                onChange={(value) => {
                  setEmbeddingModel(value);
                  setEmbeddingTestResult(null);
                }}
                models={embeddingModels}
                allowEmpty
                emptyOptionLabel="（不启用 Embedding）"
              />
              <Field
                id="embedding-timeout"
                label="Embedding 请求超时（秒）"
                value={String(embeddingTimeoutSeconds)}
                onChange={(value) =>
                  setEmbeddingTimeoutSeconds(Number(value) || 30)
                }
                type="number"
              />
              <div className="rounded-xl border border-violet-100 bg-violet-50/50 p-4">
                <h3 className="text-sm font-semibold text-slate-800">
                  3. 获取向量模型
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                  模型列表在这里获取；连接和兼容性测试统一在页面底部执行。
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={handleFetchEmbeddingModels}
                    disabled={
                      embeddingModelsState === 'fetching' ||
                      embeddingConnectionChanged ||
                      embeddingTimeoutChanged
                    }
                    className="rounded border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {embeddingModelsState === 'fetching'
                      ? '正在获取…'
                      : embeddingModels.length
                        ? '刷新向量模型列表'
                        : '获取向量模型列表'}
                  </button>
                </div>
                {embeddingModelsError && (
                  <p className="mt-3 text-sm text-red-700">
                    {embeddingModelsError}
                  </p>
                )}
                {(embeddingConnectionChanged ||
                  embeddingModelChanged ||
                  embeddingTimeoutChanged) && (
                  <p className="mt-3 text-sm text-amber-700">
                    请先保存向量模型参数的改动，再执行获取或测试。
                  </p>
                )}
                <p className="mt-3 text-xs text-slate-500">
                  获取列表或测试不会重新生成文档向量；确认兼容性后，再在下方决定是否构建或切换索引。
                </p>
              </div>
            </div>
          )}
        </section>
        )}

        <div className="sticky bottom-2 z-20 rounded-2xl border border-slate-200 bg-white/95 p-2.5 shadow-xl shadow-slate-950/10 backdrop-blur sm:bottom-4 sm:p-4">
          <div className="flex flex-wrap items-center justify-end gap-3 sm:justify-between">
            <div className="hidden sm:block">
              <p className="text-sm font-medium text-slate-800">
                {activePanel === 'chat' ? '对话模型操作' : '向量模型操作'}
              </p>
              <p className="mt-0.5 text-xs text-slate-500">
                {(activePanel === 'chat' ? chatDirty : embeddingDirty)
                  ? '有尚未保存的修改，保存后才能测试当前配置。'
                  : anyDirty
                    ? '另一个设置分区有尚未保存的修改。'
                  : '当前页面没有待保存的修改，可以直接进行连接测试。'}
              </p>
            </div>
            <div className="flex w-full items-center gap-2 sm:w-auto">
            <button
              type="submit"
              disabled={
                saveState === 'saving' ||
                !anyDirty
              }
              className="flex-1 rounded-xl bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:bg-slate-300 sm:flex-none"
            >
              {saveState === 'saving'
                ? '正在保存…'
                : dirtyPanelCount > 1
                  ? '保存全部修改'
                  : chatDirty
                    ? '保存对话模型'
                    : embeddingDirty
                      ? '保存向量模型'
                      : activePanel === 'chat'
                        ? '保存对话模型'
                        : '保存向量模型'}
            </button>
            {activePanel === 'chat' ? (
              <button
                type="button"
                onClick={handleTest}
                disabled={testing || chatDirty || provider === 'disabled'}
                className="flex-1 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40 sm:flex-none"
              >
                {testing ? '正在测试…' : '测试对话连接'}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleEmbeddingTest}
                disabled={
                  embeddingTesting ||
                  embeddingDirty ||
                  embeddingProvider === 'disabled' ||
                  !config.embedding_model
                }
                className="flex-1 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40 sm:flex-none"
              >
                {embeddingTesting ? '正在测试…' : '测试向量兼容性'}
              </button>
            )}
            </div>
          </div>
          <div className="mt-2">
            {saveMessage && (
              <p
                className={`text-sm ${
                  saveState === 'error' ? 'text-red-700' : 'text-slate-600'
                }`}
              >
                {saveMessage}
              </p>
            )}
            {activePanel === 'chat' && testResult && (
              <p className={`text-sm ${testResult.ok ? 'text-emerald-700' : 'text-red-700'}`}>
                {testResult.ok ? '对话模型连接正常' : `对话模型连接失败：${testResult.message}`}
              </p>
            )}
            {activePanel === 'embedding' && embeddingTestResult && (
              <p className={`text-sm ${embeddingTestResult.ok ? 'text-emerald-700' : 'text-red-700'}`}>
                {embeddingTestResult.ok
                  ? embeddingTestResult.decision === 'same'
                    ? `无需重建：${embeddingTestResult.reason}`
                    : embeddingTestResult.decision === 'compatible'
                      ? `高度兼容：${embeddingTestResult.reason}`
                      : embeddingTestResult.decision === 'unknown'
                        ? '首次使用：连接正常，后续需要创建首个向量索引'
                        : `需要重建：${embeddingTestResult.reason}`
                  : `测试失败：${embeddingTestResult.reason}`}
              </p>
            )}
          </div>
        </div>
      </form>
    </main>
  );
}

function currentChatModel(
  provider: Provider,
  openaiModel: string,
  ollamaModel: string,
) {
  if (provider === 'openai') return openaiModel || 'OpenAI 兼容';
  if (provider === 'ollama') return ollamaModel || 'Ollama / 本地';
  return '当前未启用';
}

function Field({
  id,
  label,
  value,
  onChange,
  help,
  type = 'text',
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  help?: string;
  type?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm text-slate-700">
        {label}
      </label>
      <input
        id={id}
        name={id}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
      />
      {help && <p className="mt-1 text-xs text-slate-500">{help}</p>}
    </div>
  );
}

function FieldWithList({
  id,
  label,
  value,
  onChange,
  models,
  help,
  allowEmpty = false,
  emptyOptionLabel = '',
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  models: AIModelsResponse['models'];
  help?: string;
  allowEmpty?: boolean;
  emptyOptionLabel?: string;
}) {
  const matches = models.some((model) => model.id === value);
  // The <select> can only show one of the known options. When the
  // user types a value that is not in the list, fall back to the
  // placeholder option so the widget never displays a phantom value.
  const selectValue = matches
    ? value
    : allowEmpty
      ? '__empty__'
      : '';

  const handleSelectChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const selectedValue = event.target.value;
    if (allowEmpty && selectedValue === '__empty__') {
      onChange('');
      return;
    }
    if (selectedValue) {
      onChange(selectedValue);
    }
  };

  return (
    <div>
      <label htmlFor={id} className="block text-sm text-slate-700">
        {label}
      </label>
      {models.length > 0 && (
        <select
          aria-label={`${label}快速选择`}
          className="mb-2 mt-1 w-full rounded border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
          onChange={handleSelectChange}
          value={selectValue}
        >
          {allowEmpty ? (
            <option value="__empty__">{emptyOptionLabel || '（不选择）'}</option>
          ) : (
            <option value="">
              从 {models.length} 个模型中选择
            </option>
          )}
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.label || model.id}
            </option>
          ))}
        </select>
      )}
      <input
        id={id}
        name={id}
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="手动输入或搜索特殊模型名"
        className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
      />
      {help && <p className="mt-1 text-xs text-slate-500">{help}</p>}
    </div>
  );
}
