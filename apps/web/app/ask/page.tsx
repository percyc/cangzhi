'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

type Citation = {
  id: number;
  document_id: number;
  document_version_id: number;
  chunk_id: number;
  title: string;
  heading_path: string[];
  page: number | null;
  paragraph_index: number | null;
  source_start: number | null;
  source_end: number | null;
  source_type: string;
  source_url: string | null;
  snippet: string;
};

type AskResponse = {
  question: string;
  answer: string;
  insufficient_evidence: boolean;
  citations: Citation[];
  evidence: Citation[];
  provider: string;
  model: string | null;
  retrieval?: {
    vector_used: boolean;
    degraded_reason: string | null;
  };
  scope?: { slug: string };
};

type KnowledgeScope = {
  id: number | null;
  slug: string;
  name: string;
  description: string | null;
  system: boolean;
};

type Turn = {
  id: number;
  question: string;
  response: AskResponse;
};

export default function AskPage() {
  return (
    <Suspense fallback={<AskSkeleton />}>
      <AskClient />
    </Suspense>
  );
}

function AskSkeleton() {
  return (
    <main className="mx-auto max-w-6xl px-5 py-8">
      <div className="h-8 w-48 animate-pulse rounded bg-slate-200" />
      <div className="mt-6 h-80 animate-pulse rounded-3xl bg-slate-100" />
    </main>
  );
}

function AskClient() {
  const searchParams = useSearchParams();
  const [question, setQuestion] = useState(searchParams.get('q') ?? '');
  const [scopeSlug, setScopeSlug] = useState('all');
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providerReady, setProviderReady] = useState<boolean | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    Promise.all([
      fetch('/api/v1/knowledge/scopes', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('知识范围加载失败');
          return (await response.json()) as { items: KnowledgeScope[] };
        },
      ),
      fetch('/api/ask/status', { cache: 'no-store' }).then(
        async (response) => {
          if (!response.ok) throw new Error('模型状态加载失败');
          return (await response.json()) as { provider_configured: boolean };
        },
      ),
    ])
      .then(([scopeResult, status]) => {
        setScopes(scopeResult.items);
        setProviderReady(status.provider_configured);
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : '初始化失败');
      });
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [turns, loading]);

  const runAsk = async () => {
    const trimmed = question.trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('/api/v1/knowledge/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: trimmed,
          scope_slug: scopeSlug,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = body?.detail;
        throw new Error(
          typeof detail === 'object'
            ? (detail.message ?? '问答请求失败')
            : (detail ?? '问答请求失败'),
        );
      }
      setTurns((current) => [
        ...current,
        {
          id: Date.now(),
          question: trimmed,
          response: body as AskResponse,
        },
      ]);
      setQuestion('');
      window.history.replaceState(null, '', '/ask');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '问答失败');
    } finally {
      setLoading(false);
    }
  };

  const selectedScope = scopes.find((item) => item.slug === scopeSlug);

  return (
    <main className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-7xl gap-5 px-4 py-5 lg:px-6">
      <aside className="hidden w-64 shrink-0 lg:block">
        <div className="sticky top-5 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
            当前知识范围
          </p>
          <div className="mt-3 space-y-1">
            {scopes.map((scope) => (
              <button
                key={`${scope.system ? 'system' : 'saved'}-${scope.slug}`}
                type="button"
                onClick={() => setScopeSlug(scope.slug)}
                className={`w-full rounded-xl px-3 py-2 text-left text-sm transition ${
                  scopeSlug === scope.slug
                    ? 'bg-slate-900 text-white'
                    : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {scope.name}
              </button>
            ))}
          </div>
          <p className="mt-4 border-t border-slate-100 pt-4 text-xs leading-5 text-slate-500">
            每轮都会重新检索所选范围，并保留可核验出处。当前会话只在本页保留，
            不会影响知识原文。
          </p>
          <Link
            href="/search"
            className="mt-4 block text-xs font-medium text-slate-700 hover:text-slate-950"
          >
            切换到精确搜索 →
          </Link>
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-7">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-950">
              问知识库
            </h1>
            <p className="mt-1 text-xs text-slate-500">
              {selectedScope?.name ?? '全部知识'} · 检索证据后回答
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={scopeSlug}
              onChange={(event) => setScopeSlug(event.target.value)}
              className="max-w-48 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 lg:hidden"
              aria-label="知识范围"
            >
              {scopes.map((scope) => (
                <option key={scope.slug} value={scope.slug}>
                  {scope.name}
                </option>
              ))}
            </select>
            {turns.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  setTurns([]);
                  setError(null);
                }}
                className="rounded-xl border border-slate-200 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50"
              >
                新对话
              </button>
            )}
          </div>
        </header>

        <div className="min-h-[32rem] flex-1 overflow-y-auto bg-slate-50/60 px-4 py-6 sm:px-7">
          {turns.length === 0 && !loading && (
            <WelcomeState
              disabled={providerReady === false}
              onExample={setQuestion}
            />
          )}
          <div className="space-y-8">
            {turns.map((turn) => (
              <ConversationTurn key={turn.id} turn={turn} />
            ))}
            {loading && (
              <div className="flex gap-3">
                <AssistantMark />
                <div className="rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 shadow-sm">
                  正在检索、核对出处并组织回答…
                </div>
              </div>
            )}
          </div>
          <div ref={endRef} />
        </div>

        <footer className="border-t border-slate-100 bg-white p-4 sm:px-7 sm:py-5">
          {providerReady === false && (
            <p className="mb-3 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-800">
              尚未配置对话模型。
              <Link href="/settings" className="ml-1 font-medium underline">
                前往设置
              </Link>
            </p>
          )}
          {error && (
            <p className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </p>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void runAsk();
            }}
            className="flex items-end gap-3 rounded-2xl border border-slate-200 bg-slate-50 p-2 focus-within:border-slate-400 focus-within:bg-white"
          >
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === 'Enter' &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  void runAsk();
                }
              }}
              placeholder="询问你保存过的资料…"
              rows={2}
              maxLength={500}
              className="max-h-40 min-h-12 flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-6 text-slate-900 outline-none placeholder:text-slate-400"
            />
            <button
              type="submit"
              disabled={!question.trim() || loading || providerReady === false}
              className="rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              发送
            </button>
          </form>
          <p className="mt-2 text-center text-[11px] text-slate-400">
            Enter 发送 · Shift + Enter 换行 · 回答可能有误，请核对引用原文
          </p>
        </footer>
      </section>
    </main>
  );
}

function WelcomeState({
  disabled,
  onExample,
}: {
  disabled: boolean;
  onExample: (value: string) => void;
}) {
  const examples = [
    '总结我收藏过的向量检索方案',
    '我保存的资料里，如何避免知识切片截断？',
    '比较几篇资料对个人知识管理的不同观点',
  ];
  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center py-16 text-center">
      <AssistantMark large />
      <h2 className="mt-5 text-2xl font-semibold text-slate-900">
        从自己的知识出发
      </h2>
      <p className="mt-2 max-w-lg text-sm leading-6 text-slate-500">
        藏知会在当前范围中同时使用关键词和可用的向量索引检索，并把答案链接回原文。
      </p>
      <div className="mt-7 grid w-full gap-2 sm:grid-cols-3">
        {examples.map((example) => (
          <button
            key={example}
            type="button"
            disabled={disabled}
            onClick={() => onExample(example)}
            className="rounded-2xl border border-slate-200 bg-white p-3 text-left text-xs leading-5 text-slate-600 shadow-sm hover:border-slate-300 hover:text-slate-900 disabled:opacity-50"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}

function ConversationTurn({ turn }: { turn: Turn }) {
  return (
    <article className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-slate-900 px-4 py-3 text-sm leading-6 text-white">
          {turn.question}
        </div>
      </div>
      <div className="flex items-start gap-3">
        <AssistantMark />
        <div className="min-w-0 max-w-3xl flex-1">
          <div className="prose prose-slate max-w-none rounded-2xl rounded-tl-md border border-slate-200 bg-white px-5 py-4 text-sm leading-7 shadow-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>
              {turn.response.answer}
            </ReactMarkdown>
          </div>
          <ResultMeta response={turn.response} />
          {turn.response.citations.length > 0 && (
            <CitationList citations={turn.response.citations} />
          )}
        </div>
      </div>
    </article>
  );
}

function ResultMeta({ response }: { response: AskResponse }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-400">
      <span>
        {response.retrieval?.vector_used ? '混合检索' : '关键词检索'}
      </span>
      {response.provider && (
        <span>
          {response.provider}
          {response.model ? ` · ${response.model}` : ''}
        </span>
      )}
      {response.insufficient_evidence && (
        <span className="text-amber-700">证据不足</span>
      )}
      {response.retrieval?.degraded_reason && (
        <span className="text-amber-700">
          {response.retrieval.degraded_reason}
        </span>
      )}
    </div>
  );
}

function CitationList({ citations }: { citations: Citation[] }) {
  return (
    <details className="mt-3 rounded-2xl border border-slate-200 bg-white">
      <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-slate-600">
        查看 {citations.length} 条引用
      </summary>
      <ol className="border-t border-slate-100">
        {citations.map((citation, index) => (
          <li
            key={`${citation.chunk_id}-${citation.id}`}
            className="border-b border-slate-100 p-4 last:border-b-0"
          >
            <div className="flex items-start gap-3">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-600">
                {index + 1}
              </span>
              <div className="min-w-0">
                <Link
                  href={`/documents/${citation.document_id}`}
                  className="text-sm font-semibold text-slate-800 hover:underline"
                >
                  {citation.title}
                </Link>
                <p className="mt-1 line-clamp-3 text-xs leading-5 text-slate-500">
                  {citation.snippet}
                </p>
                <p className="mt-2 text-[11px] text-slate-400">
                  {citation.heading_path.length > 0
                    ? citation.heading_path.join(' › ')
                    : sourceTypeLabel(citation.source_type)}
                  {citation.page ? ` · 第 ${citation.page} 页` : ''}
                </p>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function AssistantMark({ large = false }: { large?: boolean }) {
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-slate-800 to-slate-950 font-semibold text-white shadow-sm ${
        large ? 'h-12 w-12 text-lg' : 'h-8 w-8 text-xs'
      }`}
      aria-hidden="true"
    >
      知
    </span>
  );
}

function sourceTypeLabel(value: string) {
  return (
    {
      note: '随手记',
      file: '文件',
      url: '网页',
    }[value] ?? value
  );
}
