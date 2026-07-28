'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import {
  fetchAIConfig,
  fetchAIModels,
  testAIConfig,
  updateAIConfig,
  type AIConfig,
  type AIModelsResponse,
  type AITestResult,
} from '@/lib/api';

type Provider = AIConfig['provider'];
type SaveState = 'idle' | 'saving' | 'saved' | 'error';
type FetchState = 'idle' | 'fetching' | 'success' | 'error';

export default function SettingsPage() {
  const router = useRouter();
  const [config, setConfig] = useState<AIConfig | null>(null);
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
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : '读取设置失败');
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

    const payload = {
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
    };

    try {
      const next = await updateAIConfig(payload);
      setConfig(next);
      invalidateModels();
      setApiKey('');
      setClearKey(false);
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
    <main className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="text-2xl font-semibold text-slate-900">模型设置</h1>
      <p className="mt-2 text-sm text-slate-500">
        这里的设置会立即生效，覆盖环境变量里的默认值。密钥加密保存在服务器上，页面只显示是否已保存。
      </p>

      <form className="mt-8 space-y-8" onSubmit={handleSave}>
        <section>
          <h2 className="text-base font-semibold text-slate-800">模型来源</h2>
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
        </section>

        {provider === 'openai' && (
          <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-4">
            <h2 className="text-base font-semibold text-slate-800">
              OpenAI 兼容配置
            </h2>
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
          <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-4">
            <h2 className="text-base font-semibold text-slate-800">
              Ollama 配置
            </h2>
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

        <section>
          <Field
            id="timeout"
            label="请求超时（秒）"
            value={String(timeoutSeconds)}
            onChange={(value) => setTimeoutSeconds(Number(value) || 30)}
            type="number"
          />
        </section>

        {provider !== 'disabled' && (
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={handleFetchModels}
              disabled={modelsFetchState === 'fetching' || connectionChanged}
              className="rounded border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {modelsFetchState === 'fetching'
                ? '正在获取模型列表…'
                : models.length
                  ? '刷新模型列表'
                  : '获取模型列表'}
            </button>
            {connectionChanged && (
              <p className="text-sm text-amber-700">
                请先保存地址、密钥或模型来源的改动
              </p>
            )}
            {modelsFetchError && (
              <p className="text-sm text-red-700">
                获取失败：{modelsFetchError}
              </p>
            )}
            {modelsFetchState === 'success' && models.length > 0 && (
              <p className="text-sm text-emerald-700">
                已获取 {models.length} 个模型，可在模型名称输入框中选择
              </p>
            )}
            {modelsFetchState === 'success' && models.length === 0 && (
              <p className="text-sm text-slate-600">
                服务返回空列表，请手动输入模型名称
              </p>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={saveState === 'saving'}
            className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-800 disabled:bg-slate-400"
          >
            {saveState === 'saving' ? '正在保存…' : '保存设置'}
          </button>
          <button
            type="button"
            onClick={handleTest}
            disabled={
              testing ||
              provider === 'disabled' ||
              connectionChanged ||
              modelChanged
            }
            className="rounded border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {testing ? '正在测试…' : '测试连接'}
          </button>
          {saveMessage && (
            <p
              className={`text-sm ${
                saveState === 'error' ? 'text-red-700' : 'text-slate-600'
              }`}
            >
              {saveMessage}
            </p>
          )}
          {testResult && (
            <p
              className={`text-sm ${
                testResult.ok ? 'text-emerald-700' : 'text-red-700'
              }`}
            >
              {testResult.ok ? '连接正常' : `连接失败：${testResult.message}`}
            </p>
          )}
        </div>
      </form>
    </main>
  );
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
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  models: AIModelsResponse['models'];
  help?: string;
}) {
  const listId = `${id}-models`;
  return (
    <div>
      <label htmlFor={id} className="block text-sm text-slate-700">
        {label}
      </label>
      <input
        id={id}
        name={id}
        type="text"
        list={listId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
      />
      {models.length > 0 && (
        <datalist id={listId}>
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.label || model.id}
            </option>
          ))}
        </datalist>
      )}
      {help && <p className="mt-1 text-xs text-slate-500">{help}</p>}
    </div>
  );
}
