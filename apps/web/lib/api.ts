export type AuthStatus = {
  authenticated: boolean;
  setup_required: boolean;
  admin: { id: number; username: string } | null;
  session?: { id: number; expires_at: string };
};

export type AIConfig = {
  provider: 'disabled' | 'openai' | 'ollama';
  openai_base_url: string | null;
  openai_model: string | null;
  has_api_key: boolean;
  ollama_base_url: string | null;
  ollama_model: string | null;
  embedding_model: string | null;
  timeout_seconds: number;
  prompt_version: string;
  updated_at: string | null;
};

export type AIConfigResponse = { config: AIConfig };

export type AIConfigPayload = {
  provider: 'disabled' | 'openai' | 'ollama';
  openai?: {
    base_url: string;
    model: string;
  };
  ollama?: {
    base_url: string;
    model: string;
  };
  api_key_action: 'keep' | 'replace' | 'clear';
  api_key?: string;
  timeout_seconds?: number;
  embedding_model?: string | null;
};

export type AITestResult = {
  ok: boolean;
  message: string;
};

export type AIModelsResponse = {
  models: Array<{ id: string; label?: string }>;
  provider: string;
  current_model: string;
};

export async function fetchAIModels(): Promise<AIModelsResponse> {
  const response = await fetch('/api/settings/ai/models', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '获取模型列表失败'));
  }
  return parseJson<AIModelsResponse>(response);
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

function extractDetailMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string' && message) return message;
    }
    if (typeof detail === 'string' && detail) return detail;
  }
  return fallback;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const response = await fetch('/api/auth/status', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error('暂时无法读取登录状态');
  }
  return parseJson<AuthStatus>(response);
}

export async function fetchAIConfig(): Promise<AIConfig> {
  const response = await fetch('/api/settings/ai', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '读取模型设置失败'));
  }
  const body = await parseJson<AIConfigResponse>(response);
  return body.config;
}

export async function updateAIConfig(
  payload: AIConfigPayload,
): Promise<AIConfig> {
  const response = await fetch('/api/settings/ai', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '保存模型设置失败'));
  }
  const body = await parseJson<AIConfigResponse>(response);
  return body.config;
}

export async function testAIConfig(): Promise<AITestResult> {
  const response = await fetch('/api/settings/ai/test', {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '测试连接失败'));
  }
  return parseJson<AITestResult>(response);
}

export async function testEmbeddingConfig(): Promise<AITestResult> {
  const response = await fetch('/api/settings/ai/embedding/test', {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '测试 Embedding 连接失败'));
  }
  return parseJson<AITestResult>(response);
}

export async function askStatus(): Promise<{
  provider_configured: boolean;
  provider: string;
}> {
  const response = await fetch('/api/ask/status', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error('读取问答状态失败');
  }
  return parseJson(response);
}

async function extractErrorMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const payload = await parseJson<unknown>(response);
    return extractDetailMessage(payload, fallback);
  } catch {
    return fallback;
  }
}
