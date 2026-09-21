'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import {
  discoverAIModels,
  fetchAIConfig,
  fetchEmbeddingStatus,
  runEmbeddingProfileAction,
  testAIConfig,
  testEmbeddingCompatibility,
  testOcrConfig,
  updateAIConfig,
  updateOcrConfig,
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
type OcrProvider = AIConfig['ocr_provider'];
type SaveState = 'idle' | 'saving' | 'saved' | 'error';
type FetchState = 'idle' | 'fetching' | 'success' | 'error';
type SettingsPanel = Extract<SettingsSection, 'chat' | 'embedding' | 'ocr'>;

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
  // --- External visual OCR channel (OCR stage 2) -----------------------
  const [ocrProvider, setOcrProvider] = useState<OcrProvider>('disabled');
  const [ocrBaseUrl, setOcrBaseUrl] = useState('');
  const [ocrModel, setOcrModel] = useState('');
  const [ocrApiKey, setOcrApiKey] = useState('');
  const [clearOcrApiKey, setClearOcrApiKey] = useState(false);
  const [ocrTimeoutSeconds, setOcrTimeoutSeconds] = useState(30);
  const [ocrConfidenceThreshold, setOcrConfidenceThreshold] = useState(600);
  const [ocrMinChars, setOcrMinChars] = useState(8);
  const [ocrMaxExternalPages, setOcrMaxExternalPages] = useState(20);
  const [ocrReuseChat, setOcrReuseChat] = useState(false);
  const [ocrTestResult, setOcrTestResult] = useState<AITestResult | null>(null);
  const [ocrTesting, setOcrTesting] = useState(false);
  const [ocrModels, setOcrModels] = useState<AIModelsResponse['models']>([]);
  const [ocrModelsState, setOcrModelsState] = useState<FetchState>('idle');
  const [ocrModelsError, setOcrModelsError] = useState<string | null>(null);

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
        setOcrProvider(data.ocr_provider);
        setOcrBaseUrl(
          data.ocr_base_url ?? data.openai_base_url ?? 'https://api.openai.com/v1',
        );
        setOcrModel(data.ocr_model ?? '');
        setOcrTimeoutSeconds(data.ocr_timeout_seconds || 30);
        setOcrConfidenceThreshold(data.ocr_confidence_threshold ?? 60);
        setOcrMinChars(data.ocr_min_chars ?? 8);
        setOcrMaxExternalPages(data.ocr_max_external_pages ?? 20);
        setOcrReuseChat(false);
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
    const syncPanelFromLocation = () => {
      const requested = new URLSearchParams(window.location.search).get('section');
      const hash = window.location.hash.replace(/^#/, '');
      if (requested === 'embedding' || hash === 'embedding') {
        setActivePanel('embedding');
      } else if (requested === 'ocr' || hash === 'ocr') {
        setActivePanel('ocr');
      } else {
        setActivePanel('chat');
      }
    };
    syncPanelFromLocation();
    window.addEventListener('popstate', syncPanelFromLocation);
    window.addEventListener('hashchange', syncPanelFromLocation);
    return () => {
      window.removeEventListener('popstate', syncPanelFromLocation);
      window.removeEventListener('hashchange', syncPanelFromLocation);
    };
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

  const invalidateEmbeddingModels = () => {
    setEmbeddingModels([]);
    setEmbeddingModelsState('idle');
    setEmbeddingModelsError(null);
  };

  const invalidateOcrModels = () => {
    setOcrModels([]);
    setOcrModelsState('idle');
    setOcrModelsError(null);
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
  // The OCR panel is tracked independently: a change to the chat
  // model or to the embedding channel never invalidates the OCR
  // test, and vice versa. The only field that touches both is
  // ``ocr_reuse_chat`` which is only sent once on the OCR panel.
  const ocrConnectionChanged =
    ocrProvider !== config?.ocr_provider ||
    (ocrProvider === 'openai' &&
      ocrBaseUrl.trim() !== (config?.ocr_base_url ?? '')) ||
    Boolean(ocrApiKey) ||
    clearOcrApiKey;
  const ocrModelChanged =
    ocrProvider === 'openai' &&
    ocrModel.trim() !== (config?.ocr_model ?? '');
  const ocrParamsChanged =
    ocrTimeoutSeconds !== (config?.ocr_timeout_seconds || 30) ||
    ocrConfidenceThreshold !== (config?.ocr_confidence_threshold ?? 600) ||
    ocrMinChars !== (config?.ocr_min_chars ?? 8) ||
    ocrMaxExternalPages !== (config?.ocr_max_external_pages ?? 20) ||
    ocrReuseChat;
  const ocrDirty =
    ocrConnectionChanged || ocrModelChanged || ocrParamsChanged;
  const dirtyPanelCount =
    Number(chatDirty) + Number(embeddingDirty) + Number(ocrDirty);
  const anyDirty = dirtyPanelCount > 0;
  const activePanelDirty = activePanel === 'chat'
    ? chatDirty
    : activePanel === 'embedding'
      ? embeddingDirty
      : ocrDirty;
  const chatDiscoveryReady = provider === 'ollama'
    ? Boolean(ollamaBaseUrl.trim())
    : provider === 'openai' && Boolean(
        openaiBaseUrl.trim() && (apiKey || (config?.has_api_key && !clearKey)),
      );
  const embeddingDiscoveryReady = embeddingProvider === 'ollama'
    ? Boolean(embeddingBaseUrl.trim())
    : embeddingProvider === 'openai' && Boolean(
        embeddingBaseUrl.trim() && (
          embeddingApiKey || (config?.has_embedding_api_key && !clearEmbeddingApiKey)
        ),
      );
  const ocrDiscoveryReady = ocrProvider === 'openai' && Boolean(
    ocrBaseUrl.trim() && (
      ocrReuseChat
        ? apiKey || (config?.has_api_key && !clearKey)
        : ocrApiKey || (config?.has_ocr_api_key && !clearOcrApiKey)
    ),
  );

  const handleFetchModels = async () => {
    setModelsFetchState('fetching');
    setModelsFetchError(null);
    try {
      const result = await discoverAIModels({
        channel: 'chat',
        provider: provider === 'ollama' ? 'ollama' : 'openai',
        base_url: provider === 'ollama' ? ollamaBaseUrl.trim() : openaiBaseUrl.trim(),
        api_key: provider === 'openai' ? apiKey || undefined : undefined,
        use_saved_api_key: provider === 'openai' ? !clearKey : undefined,
        timeout_seconds: timeoutSeconds,
      });
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
    if (activePanel === 'ocr') {
      await handleOcrSave();
      return;
    }
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
    setEmbeddingModelsState('fetching');
    setEmbeddingModelsError(null);
    try {
      const result = await discoverAIModels({
        channel: 'embedding',
        provider: embeddingProvider === 'ollama' ? 'ollama' : 'openai',
        base_url: embeddingBaseUrl.trim(),
        api_key: embeddingProvider === 'openai' ? embeddingApiKey || undefined : undefined,
        use_saved_api_key: embeddingProvider === 'openai' ? !clearEmbeddingApiKey : undefined,
        timeout_seconds: embeddingTimeoutSeconds,
      });
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
      // 启用时服务端可能发现新切片并自动补建。即使本次
      // 启用返回 409，也要立即刷新为 building，让轮询接管进度展示。
      try {
        setEmbeddingStatus(await fetchEmbeddingStatus());
      } catch {
        // 保留原始操作错误，状态刷新失败不应覆盖它。
      }
    } finally {
      setEmbeddingActionId(null);
    }
  };

  const handleFetchOcrModels = async () => {
    setOcrModelsState('fetching');
    setOcrModelsError(null);
    try {
      const result = await discoverAIModels({
        channel: ocrReuseChat ? 'chat' : 'ocr',
        provider: 'openai',
        base_url: ocrBaseUrl.trim(),
        api_key: (ocrReuseChat ? apiKey : ocrApiKey) || undefined,
        use_saved_api_key: ocrReuseChat ? !clearKey : !clearOcrApiKey,
        timeout_seconds: ocrTimeoutSeconds,
      });
      setOcrModels(result.models);
      setOcrModelsState('success');
    } catch (err) {
      setOcrModelsState('error');
      setOcrModelsError(
        err instanceof Error ? err.message : '获取外部 OCR 模型列表失败',
      );
    }
  };

  const handleOcrTest = async () => {
    setOcrTesting(true);
    setOcrTestResult(null);
    try {
      const result = await testOcrConfig();
      setOcrTestResult(result);
    } catch (err) {
      setOcrTestResult({
        ok: false,
        message: err instanceof Error ? err.message : '测试外部 OCR 连接失败',
      });
    } finally {
      setOcrTesting(false);
    }
  };

  const handleOcrSave = async () => {
    if (!config) return;
    setSaveState('saving');
    setSaveMessage(null);
    setOcrTestResult(null);
    try {
      const next = await updateOcrConfig({
        provider: ocrProvider,
        base_url: ocrProvider === 'openai' ? ocrBaseUrl.trim() : null,
        model: ocrProvider === 'openai' ? ocrModel.trim() : null,
        api_key_action: (clearOcrApiKey
          ? 'clear'
          : ocrApiKey
            ? 'replace'
            : 'keep') as 'keep' | 'replace' | 'clear',
        api_key: clearOcrApiKey ? undefined : ocrApiKey || undefined,
        timeout_seconds: ocrTimeoutSeconds,
        confidence_threshold: ocrConfidenceThreshold,
        min_chars: ocrMinChars,
        max_external_pages: ocrMaxExternalPages,
        reuse_chat: ocrReuseChat || undefined,
      });
      setConfig(next);
      setOcrApiKey('');
      setClearOcrApiKey(false);
      setOcrReuseChat(false);
      setOcrModels([]);
      setOcrModelsState('idle');
      setSaveState('saved');
      setSaveMessage('外部 OCR 设置已保存');
      router.refresh();
    } catch (err) {
      setSaveState('error');
      setSaveMessage(err instanceof Error ? err.message : '保存失败');
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
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
        系统设置
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">
        {activePanel === 'chat'
          ? '对话模型'
          : activePanel === 'embedding'
            ? '向量与索引'
            : '图片文字识别'}
      </h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">
        {activePanel === 'chat'
          ? '配置用于分类、摘要、整理和知识问答的大模型。'
          : activePanel === 'embedding'
            ? '配置语义向量渠道，并管理全库索引版本与切换。'
            : '为扫描 PDF 中的图片页配置外部视觉识别模型。'}
      </p>
      {provider === 'disabled' && !embeddingStatus?.active_profile.id && (
        <section className="mt-5 rounded-2xl border border-blue-200 bg-blue-50/60 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-semibold text-blue-950">第一次使用？先配置对话模型即可</p>
              <p className="mt-1 text-xs leading-5 text-blue-800">
                对话模型负责摘要、分类和问答；向量模型与图片文字识别都是可选增强，之后再配置也不影响先上传资料。
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2 text-xs font-medium text-blue-900">
              <span className="rounded-full bg-white px-3 py-1.5">1 填连接</span>
              <span>→</span>
              <span className="rounded-full bg-white px-3 py-1.5">2 选模型</span>
              <span>→</span>
              <span className="rounded-full bg-white px-3 py-1.5">3 保存测试</span>
            </div>
          </div>
        </section>
      )}
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
          ocr:
            ocrProvider === 'disabled'
              ? '当前未启用'
              : `外部 OCR · ${ocrModel || '未选模型'}`,
        }}
        dirty={{ chat: chatDirty, embedding: embeddingDirty, ocr: ocrDirty }}
        onSelect={(section) => {
          if (
            section === 'chat' ||
            section === 'embedding' ||
            section === 'ocr'
          ) {
            setActivePanel(section);
          }
        }}
        beforeNavigate={(section) =>
          ['chat', 'embedding', 'ocr'].includes(section) && section === activePanel
            ? true
            : section === activePanel ||
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
            <FieldWithList
              id="openai-model"
              label="模型名称"
              help="获取列表是可选的；如果服务不提供模型列表，可以直接输入模型名称。"
              value={openaiModel}
              onChange={setOpenaiModel}
              models={models}
              action={{
                label: models.length ? '刷新模型列表' : '获取模型列表',
                busyLabel: '正在获取…',
                busy: modelsFetchState === 'fetching',
                disabled: !chatDiscoveryReady,
                onClick: () => void handleFetchModels(),
              }}
            />
            {modelsFetchError && <p className="text-sm text-red-700">获取失败：{modelsFetchError}</p>}
            {modelsFetchState === 'success' && <p className="text-sm text-emerald-700">已获取 {models.length} 个模型，可以从列表选择或继续手动输入。</p>}
            {!chatDiscoveryReady && <p className="text-xs text-amber-700">填写 API 地址和密钥后即可获取模型列表，无需先保存。</p>}
            <Field
              id="timeout"
              label="请求超时（秒）"
              value={String(timeoutSeconds)}
              onChange={(value) => setTimeoutSeconds(Number(value) || 30)}
              type="number"
            />
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
              help="获取列表是可选的；也可以直接输入本地模型名称。"
              value={ollamaModel}
              onChange={setOllamaModel}
              models={models}
              action={{
                label: models.length ? '刷新模型列表' : '获取模型列表',
                busyLabel: '正在获取…',
                busy: modelsFetchState === 'fetching',
                disabled: !chatDiscoveryReady,
                onClick: () => void handleFetchModels(),
              }}
            />
            {modelsFetchError && <p className="text-sm text-red-700">获取失败：{modelsFetchError}</p>}
            {modelsFetchState === 'success' && <p className="text-sm text-emerald-700">已获取 {models.length} 个本地模型。</p>}
            <Field
              id="timeout"
              label="请求超时（秒）"
              value={String(timeoutSeconds)}
              onChange={(value) => setTimeoutSeconds(Number(value) || 30)}
              type="number"
            />
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
                  onClick={() => {
                    setEmbeddingProvider(value);
                    invalidateEmbeddingModels();
                  }}
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
                onChange={(value) => {
                  setEmbeddingBaseUrl(value);
                  invalidateEmbeddingModels();
                }}
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
                      invalidateEmbeddingModels();
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
                        invalidateEmbeddingModels();
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
                help="获取列表是可选的；也可以直接输入模型名称。留空即关闭 Embedding。"
                value={embeddingModel}
                onChange={(value) => {
                  setEmbeddingModel(value);
                  setEmbeddingTestResult(null);
                }}
                models={embeddingModels}
                allowEmpty
                emptyOptionLabel="（不启用 Embedding）"
                action={{
                  label: embeddingModels.length ? '刷新模型列表' : '获取模型列表',
                  busyLabel: '正在获取…',
                  busy: embeddingModelsState === 'fetching',
                  disabled: !embeddingDiscoveryReady,
                  onClick: () => void handleFetchEmbeddingModels(),
                }}
              />
              {embeddingModelsError && <p className="text-sm text-red-700">获取失败：{embeddingModelsError}</p>}
              {embeddingModelsState === 'success' && <p className="text-sm text-emerald-700">已获取 {embeddingModels.length} 个模型，可以从列表选择或手动输入。</p>}
              {!embeddingDiscoveryReady && <p className="text-xs text-amber-700">填写服务地址和密钥后即可获取模型列表，无需先保存。</p>}
              <Field
                id="embedding-timeout"
                label="Embedding 请求超时（秒）"
                value={String(embeddingTimeoutSeconds)}
                onChange={(value) =>
                  setEmbeddingTimeoutSeconds(Number(value) || 30)
                }
                type="number"
              />
              <p className="rounded-lg bg-violet-50 px-3 py-2 text-xs leading-5 text-violet-800">
                获取列表不会保存设置或生成文档向量。保存并测试兼容性后，再决定是否构建或切换索引。
              </p>
            </div>
          )}
        </section>
        )}

        {activePanel === 'ocr' && (
        <section className="space-y-5 rounded-2xl border-2 border-emerald-200 bg-white p-5 shadow-sm">
          <header className="border-b border-emerald-100 pb-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-600">
              扫描 PDF 的图片文字兜底
            </p>
            <h2 className="mt-1 text-xl font-semibold text-slate-900">
              图片文字识别
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              保留 tesseract 本地识别作为第一通道；只有本地识别失败、字符数过少
              或平均置信度低于阈值时，才把页面渲染成 PNG 发送给外部视觉模型。
            </p>
          </header>

          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
            <p className="font-semibold">隐私与成本提示</p>
            <p className="mt-1">
              外部 OCR 启用后，扫描 PDF 中被识别为图片的页面会按
              <code className="mx-1 rounded bg-amber-100 px-1">OCR_MAX_EXTERNAL_PAGES</code>
              上限发送到所选 OpenAI 兼容服务。
              每页只会发送渲染后的位图和 OCR 请求提示词；模型回答与
              <code className="mx-1 rounded bg-amber-100 px-1">pdf_extraction</code>
              元数据保留在藏知本地，不会写入日志。密钥使用 Fernet 加密保存，原始 PNG 与上游响应不会写入日志。
              处理过程中如需关闭，直接选择“不使用”并保存即可，Worker 会立即回到纯本地模式。
            </p>
          </div>

          <div>
            <h3 className="text-sm font-semibold text-slate-800">
              1. 选择外部 OCR 模型来源
            </h3>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {(['disabled', 'openai'] as const).map((value) => {
                const labels: Record<OcrProvider, { title: string; hint: string }> = {
                  disabled: {
                    title: '不使用',
                    hint: '只使用本地 tesseract，不再外发图片',
                  },
                  openai: {
                    title: 'OpenAI 兼容',
                    hint: '需要支持视觉问答的多模态模型',
                  },
                };
                const label = labels[value];
                const active = ocrProvider === value;
                const style = active
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50';
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() => {
                      setOcrProvider(value);
                      invalidateOcrModels();
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

          {ocrProvider === 'openai' && (
            <section className="space-y-4 rounded-xl bg-slate-50 p-4">
              <h3 className="text-sm font-semibold text-slate-800">
                2. 填写外部 OCR 参数
              </h3>
              <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={ocrReuseChat}
                    onChange={(event) => {
                      setOcrReuseChat(event.target.checked);
                      invalidateOcrModels();
                    }}
                  />
                  <span>
                    复用对话渠道的地址和已保存密钥
                    <span className="mt-0.5 block text-[11px] text-slate-500">
                      勾选后保存时会用对话渠道的 OpenAI 兼容地址和密钥填充外部 OCR。
                      OCR 模型名称仍然独立，可以单独指定多模态模型。
                    </span>
                  </span>
                </label>
              </div>
              <Field
                id="ocr-base-url"
                label="OpenAI 兼容服务地址"
                help="形如 https://api.openai.com/v1"
                value={ocrBaseUrl}
                onChange={(value) => {
                  setOcrBaseUrl(value);
                  invalidateOcrModels();
                }}
              />
              <div>
                <label
                  htmlFor="ocr-api-key"
                  className="block text-sm text-slate-700"
                >
                  外部 OCR 密钥
                </label>
                <input
                  id="ocr-api-key"
                  name="ocr-api-key"
                  type="password"
                  value={ocrApiKey}
                  onChange={(event) => {
                    setOcrApiKey(event.target.value);
                    invalidateOcrModels();
                    if (event.target.value) setClearOcrApiKey(false);
                  }}
                  placeholder={
                    config?.has_ocr_api_key
                      ? '已保存（输入新值以替换）'
                      : ocrReuseChat
                        ? '勾选“复用对话渠道”后无需再次输入'
                        : '请输入密钥'
                  }
                  autoComplete="off"
                  className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
                />
                <p className="mt-1 text-xs text-slate-500">
                  {config?.has_ocr_api_key
                    ? '已保存一份外部 OCR 密钥。输入新值会替换，留空则保持原密钥。'
                    : '尚未保存外部 OCR 密钥。'}
                </p>
                <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-600">
                  <input
                    type="checkbox"
                    checked={clearOcrApiKey}
                    onChange={(event) => {
                      setClearOcrApiKey(event.target.checked);
                      invalidateOcrModels();
                      if (event.target.checked) setOcrApiKey('');
                    }}
                  />
                  明确清除已保存的外部 OCR 密钥
                </label>
              </div>
              <FieldWithList
                id="ocr-model"
                label="视觉模型名称"
                help="需要支持图片问答。获取列表是可选的，也可以直接输入模型名称。"
                value={ocrModel}
                onChange={setOcrModel}
                models={ocrModels}
                allowEmpty
                emptyOptionLabel="（不填写，留待以后再选）"
                action={{
                  label: ocrModels.length ? '刷新模型列表' : '获取模型列表',
                  busyLabel: '正在获取…',
                  busy: ocrModelsState === 'fetching',
                  disabled: !ocrDiscoveryReady,
                  onClick: () => void handleFetchOcrModels(),
                }}
              />
              {ocrModelsError && <p className="text-sm text-red-700">获取失败：{ocrModelsError}</p>}
              {ocrModelsState === 'success' && <p className="text-sm text-emerald-700">已获取 {ocrModels.length} 个模型，可以从列表选择或手动输入。</p>}
              {!ocrDiscoveryReady && <p className="text-xs text-amber-700">填写服务地址和密钥后即可获取模型列表，无需先保存。</p>}
              <div className="grid gap-3 sm:grid-cols-2">
                <Field
                  id="ocr-timeout"
                  label="外部 OCR 请求超时（秒）"
                  value={String(ocrTimeoutSeconds)}
                  onChange={(value) =>
                    setOcrTimeoutSeconds(Number(value) || 30)
                  }
                  type="number"
                />
                <Field
                  id="ocr-confidence-threshold"
                  label="本地置信度触发阈值（0–1000）"
                  help="tesseract 平均置信度低于此值时再调用外部模型。设为 0 即关闭该触发条件。"
                  value={String(ocrConfidenceThreshold)}
                  onChange={(value) =>
                    setOcrConfidenceThreshold(Math.max(0, Math.min(1000, Number(value) || 0)))
                  }
                  type="number"
                />
                <Field
                  id="ocr-min-chars"
                  label="本地最少识别字符数"
                  help="tesseract 识别字符数低于此值时再调用外部模型。设为 0 即关闭该触发条件。"
                  value={String(ocrMinChars)}
                  onChange={(value) =>
                    setOcrMinChars(Math.max(0, Math.min(1000, Number(value) || 0)))
                  }
                  type="number"
                />
                <Field
                  id="ocr-max-external-pages"
                  label="单文档最多外发页数"
                  help="0 表示不调用外部模型；超过后该文档剩余页继续使用本地识别。"
                  value={String(ocrMaxExternalPages)}
                  onChange={(value) =>
                    setOcrMaxExternalPages(Math.max(0, Math.min(1000, Number(value) || 0)))
                  }
                  type="number"
                />
              </div>
            </section>
          )}
        </section>
        )}

        <div className="sticky bottom-2 z-20 rounded-2xl border border-slate-200 bg-white/95 p-2.5 shadow-xl shadow-slate-950/10 backdrop-blur sm:bottom-4 sm:p-4">
          <div className="flex flex-wrap items-center justify-end gap-3 sm:justify-between">
            <div className="hidden sm:block">
              <p className="text-sm font-medium text-slate-800">
                {activePanel === 'chat'
                  ? '对话模型操作'
                  : activePanel === 'embedding'
                    ? '向量模型操作'
                    : '外部 OCR 操作'}
              </p>
              <p className="mt-0.5 text-xs text-slate-500">
                {(
                  activePanel === 'chat'
                    ? chatDirty
                    : activePanel === 'embedding'
                      ? embeddingDirty
                      : ocrDirty
                )
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
                !activePanelDirty
              }
              className="flex-1 rounded-xl bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:bg-slate-300 sm:flex-none"
            >
              {saveState === 'saving'
                ? '正在保存…'
                : activePanel === 'chat'
                  ? '保存对话模型'
                  : activePanel === 'embedding'
                    ? '保存向量模型'
                    : '保存外部 OCR'}
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
            ) : activePanel === 'embedding' ? (
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
            ) : (
              <button
                type="button"
                onClick={handleOcrTest}
                disabled={
                  ocrTesting ||
                  ocrDirty ||
                  ocrProvider === 'disabled'
                }
                className="flex-1 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40 sm:flex-none"
              >
                {ocrTesting ? '正在测试…' : '测试外部 OCR 连接'}
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
            {activePanel === 'ocr' && ocrTestResult && (
              <p className={`text-sm ${ocrTestResult.ok ? 'text-emerald-700' : 'text-red-700'}`}>
                {ocrTestResult.ok ? '外部 OCR 连接正常' : `外部 OCR 连接失败：${ocrTestResult.message}`}
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
  action,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  models: AIModelsResponse['models'];
  help?: string;
  allowEmpty?: boolean;
  emptyOptionLabel?: string;
  action?: {
    label: string;
    busyLabel: string;
    busy: boolean;
    disabled?: boolean;
    onClick: () => void;
  };
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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="block text-sm text-slate-700">
          {label}
        </label>
        {action && (
          <button
            type="button"
            onClick={action.onClick}
            disabled={action.busy || action.disabled}
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {action.busy ? action.busyLabel : action.label}
          </button>
        )}
      </div>
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
