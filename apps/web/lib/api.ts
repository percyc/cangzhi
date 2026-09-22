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
  embedding_provider: 'disabled' | 'openai' | 'ollama';
  embedding_base_url: string | null;
  has_embedding_api_key: boolean;
  embedding_timeout_seconds: number;
  ocr_provider: 'disabled' | 'openai';
  ocr_base_url: string | null;
  ocr_model: string | null;
  has_ocr_api_key: boolean;
  ocr_timeout_seconds: number;
  ocr_confidence_threshold: number;
  ocr_min_chars: number;
  ocr_max_external_pages: number;
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
  embedding_provider?: 'disabled' | 'openai' | 'ollama';
  embedding_base_url?: string | null;
  embedding_api_key_action?: 'keep' | 'replace' | 'clear';
  embedding_api_key?: string;
  embedding_timeout_seconds?: number;
  ocr_provider?: 'disabled' | 'openai';
  ocr_base_url?: string | null;
  ocr_model?: string | null;
  ocr_api_key_action?: 'keep' | 'replace' | 'clear';
  ocr_api_key?: string;
  ocr_timeout_seconds?: number;
  ocr_confidence_threshold?: number;
  ocr_min_chars?: number;
  ocr_max_external_pages?: number;
  ocr_reuse_chat?: boolean;
};

export type OcrConfigPayload = {
  provider?: 'disabled' | 'openai';
  base_url?: string | null;
  model?: string | null;
  api_key_action: 'keep' | 'replace' | 'clear';
  api_key?: string;
  timeout_seconds?: number;
  confidence_threshold?: number;
  min_chars?: number;
  max_external_pages?: number;
  reuse_chat?: boolean;
};

export type AITestResult = {
  ok: boolean;
  message: string;
};

export type EmbeddingCompatibilityResult = {
  ok: boolean;
  decision: 'same' | 'compatible' | 'rebuild_required' | 'unknown';
  reason: string;
  profile: {
    id: number | null;
    status: string | null;
  };
  scores: number[];
};

export type EmbeddingStatus = {
  canary_version: string;
  active_profile: {
    id: number | null;
    status: string | null;
    model: string | null;
    dim: number | null;
    provider: string | null;
  };
  last_tested: {
    id: number | null;
    status: string | null;
    model: string | null;
    dim: number | null;
    provider: string | null;
  };
  profiles: EmbeddingProfileStatus[];
};

export type EmbeddingProfileStatus = {
  id: number;
  status: 'draft' | 'tested' | 'building' | 'ready' | 'active' | 'retired' | 'failed';
  provider: string;
  model: string;
  dim: number;
  total_chunks: number | null;
  completed_chunks: number | null;
  failed_chunks: number | null;
  pending_jobs: number;
  processing_jobs: number;
  failure_reasons: Array<{ message: string; count: number }>;
  last_error: string | null;
  is_active: boolean;
  available_actions: Array<'build' | 'retry' | 'activate' | 'rollback' | 'delete'>;
};

export type EmbeddingLifecycleResult = {
  profile_id?: number;
  active_profile_id?: number;
  previous_profile_id?: number | null;
  status?: string;
  enqueued?: number;
  total_chunks?: number;
};

export type AIModelsResponse = {
  models: Array<{ id: string; label?: string }>;
  provider: string;
  current_model: string;
};

export type ModelDiscoveryPayload = {
  channel: 'chat' | 'embedding' | 'ocr';
  provider: 'openai' | 'ollama';
  base_url: string;
  api_key?: string;
  use_saved_api_key?: boolean;
  timeout_seconds?: number;
};

export async function discoverAIModels(
  payload: ModelDiscoveryPayload,
): Promise<AIModelsResponse> {
  const response = await fetch('/api/settings/ai/models/discover', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '获取模型列表失败'));
  }
  return parseJson<AIModelsResponse>(response);
}

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

export async function fetchEmbeddingModels(): Promise<AIModelsResponse> {
  const response = await fetch('/api/settings/ai/embedding/models', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '获取 Embedding 模型列表失败'));
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

export async function testOcrConfig(): Promise<AITestResult> {
  const response = await fetch('/api/settings/ai/ocr/test', {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '测试外部 OCR 连接失败'));
  }
  return parseJson<AITestResult>(response);
}

export async function fetchOcrModels(): Promise<AIModelsResponse> {
  const response = await fetch('/api/settings/ai/ocr/models', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '获取外部 OCR 模型列表失败'));
  }
  return parseJson<AIModelsResponse>(response);
}

export async function updateOcrConfig(
  payload: OcrConfigPayload,
): Promise<AIConfig> {
  const response = await fetch('/api/settings/ai/ocr', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '保存外部 OCR 设置失败'));
  }
  const body = await parseJson<AIConfigResponse>(response);
  return body.config;
}

export async function testEmbeddingCompatibility(): Promise<EmbeddingCompatibilityResult> {
  const response = await fetch('/api/embeddings/test', {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '测试向量兼容性失败'));
  }
  return parseJson<EmbeddingCompatibilityResult>(response);
}

export async function fetchEmbeddingStatus(): Promise<EmbeddingStatus> {
  const response = await fetch('/api/embeddings/status', {
    cache: 'no-store',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '读取向量索引状态失败'));
  }
  return parseJson<EmbeddingStatus>(response);
}

export async function runEmbeddingProfileAction(
  profileId: number,
  action: 'build' | 'retry' | 'activate' | 'rollback' | 'delete',
): Promise<EmbeddingLifecycleResult> {
  const target =
    action === 'delete'
      ? `/api/embeddings/profiles/${profileId}`
      : `/api/embeddings/profiles/${profileId}/${action}`;
  const response = await fetch(target, {
    method: action === 'delete' ? 'DELETE' : 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, '向量索引操作失败'));
  }
  return parseJson<EmbeddingLifecycleResult>(response);
}

export type DatabaseSource = {
  id: number;
  workspace_id: number;
  name: string;
  engine: 'postgresql' | 'mysql';
  host: string;
  port: number;
  database_name: string;
  username: string;
  has_password: boolean;
  ssl_mode: string;
  trusted_private_network: boolean;
  is_enabled: boolean;
  status: string;
  last_error: string | null;
  last_tested_at: string | null;
  last_sync_at: string | null;
  freshness_mode: 'manual' | 'background' | 'strict';
  freshness_interval_minutes: number;
  snapshot_counts: { total: number; empty: number };
};

export type DatabaseSourcePayload = {
  name: string;
  engine: 'postgresql' | 'mysql';
  host: string;
  port: number;
  database_name: string;
  username: string;
  password?: string;
  password_action?: 'keep' | 'replace' | 'clear';
  ssl_mode: string;
  trusted_private_network: boolean;
  is_enabled?: boolean;
  freshness_mode?: 'manual' | 'background' | 'strict';
  freshness_interval_minutes?: number;
};

export type DatabaseCatalogColumn = {
  name: string;
  data_type: string;
  nullable: boolean;
  is_primary_key: boolean;
};

export type DatabaseCatalogTable = {
  schema_name: string;
  table_name: string;
  kind: string;
  imported: boolean;
  snapshot: DatabaseSnapshotSummary | null;
  columns: DatabaseCatalogColumn[];
};

export type DatabaseSnapshotSummary = {
  id: number;
  schema_name: string;
  table_name: string;
  document_id: number;
  document_title: string;
  document_deleted: boolean;
  dataset_id: number;
  row_count: number;
  snapshot_at: string | null;
  last_error: string | null;
};

export type DatabaseDeleteImpact = {
  source_id: number;
  source_name: string;
  snapshot_count: number;
  active_document_count: number;
  trashed_document_count: number;
};

export type DatabaseImportResult = {
  ok: boolean;
  status: 'imported' | 'skipped';
  skip_reason: string | null;
  removed_existing: boolean;
  document_id: number | null;
  dataset_id: number | null;
  snapshot_id: number | null;
  row_count: number;
  column_count: number;
  reused_document: boolean;
};

export type DatabaseEmptyCleanupResult = {
  ok: boolean;
  affected: number;
  deleted_artifacts: number;
  cleanup_warnings: string[];
};

export type DatabaseTestResult = {
  ok: boolean;
  message: string;
  server_version?: string;
  current_database?: string;
};

const DB_BASE = '/api/database-sources';

export async function fetchDatabaseSources(): Promise<DatabaseSource[]> {
  return apiRequest<DatabaseSource[]>(DB_BASE, undefined, '数据库来源读取失败');
}

export async function createDatabaseSource(
  payload: DatabaseSourcePayload,
): Promise<DatabaseSource> {
  return apiRequest<DatabaseSource>(
    DB_BASE,
    { method: 'POST', body: JSON.stringify(payload) },
    '添加数据库来源失败',
  );
}

export async function updateDatabaseSource(
  id: number,
  payload: Partial<DatabaseSourcePayload>,
): Promise<DatabaseSource> {
  return apiRequest<DatabaseSource>(
    `${DB_BASE}/${id}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
    '保存数据库来源失败',
  );
}

export async function deleteDatabaseSource(
  id: number,
  documentAction: 'keep' | 'trash',
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(
    `${DB_BASE}/${id}?document_action=${documentAction}`,
    { method: 'DELETE' },
    '删除数据库来源失败',
  );
}

export async function fetchDatabaseDeleteImpact(
  id: number,
): Promise<DatabaseDeleteImpact> {
  return apiRequest<DatabaseDeleteImpact>(
    `${DB_BASE}/${id}/delete-impact`,
    undefined,
    '删除影响核对失败',
  );
}

export async function testDatabaseSource(id: number): Promise<DatabaseTestResult> {
  return apiRequest<DatabaseTestResult>(
    `${DB_BASE}/${id}/test`,
    { method: 'POST' },
    '测试连接失败',
  );
}

export async function fetchDatabaseSchemas(id: number): Promise<string[]> {
  return apiRequest<string[]>(`${DB_BASE}/${id}/schemas`, undefined, '读取 Schema 失败');
}

export async function fetchDatabaseCatalog(
  id: number,
  schema: string,
): Promise<DatabaseCatalogTable[]> {
  return apiRequest<DatabaseCatalogTable[]>(
    `${DB_BASE}/${id}/catalog?schema=${encodeURIComponent(schema)}`,
    undefined,
    '读取表结构失败',
  );
}

export async function fetchDatabaseSnapshots(
  id: number,
): Promise<DatabaseSnapshotSummary[]> {
  return apiRequest<DatabaseSnapshotSummary[]>(
    `${DB_BASE}/${id}/snapshots`,
    undefined,
    '读取已导入表失败',
  );
}

export async function importDatabaseTable(
  id: number,
  schema_name: string,
  table: string,
  signal?: AbortSignal,
): Promise<DatabaseImportResult> {
  return apiRequest<DatabaseImportResult>(
    `${DB_BASE}/${id}/tables/import`,
    { method: 'POST', body: JSON.stringify({ schema_name, table }), signal },
    '导入快照失败',
  );
}

export async function deleteEmptyDatabaseSnapshots(
  id: number,
): Promise<DatabaseEmptyCleanupResult> {
  return apiRequest<DatabaseEmptyCleanupResult>(
    `${DB_BASE}/${id}/snapshots/empty`,
    { method: 'DELETE' },
    '清理空快照失败',
  );
}

async function apiRequest<T>(
  url: string,
  init?: RequestInit,
  fallback?: string,
): Promise<T> {
  const response = await fetch(url, {
    cache: 'no-store',
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  if (!response.ok) {
    throw new Error(await extractErrorMessage(response, fallback ?? '操作失败'));
  }
  return parseJson<T>(response);
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
