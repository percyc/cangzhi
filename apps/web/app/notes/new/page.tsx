'use client'

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { apiErrorMessage } from '@/lib/usability';
import { NoteDraftStatus } from '@/components/note-draft-status';
import { browserDraftStorage, clearNoteDraft, DraftWriteStatus, NOTE_DRAFT_TTL_MS, noteDraftKey, noteWorkspace, readNoteDraft, writeNoteDraft } from '@/lib/note-drafts';
import { runWithoutNavigationGuard, useUnsavedChanges } from '@/lib/navigation-guard';

export default function NewNotePage() {
  const router = useRouter();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [draftStatus, setDraftStatus] = useState<DraftWriteStatus>('idle');
  const [restoredAt, setRestoredAt] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const [loginRequired, setLoginRequired] = useState(false);
  const [savedDestination, setSavedDestination] = useState<string | null>(null);
  const [draftExpiresAt, setDraftExpiresAt] = useState<number | null>(null);
  const scopeRef = useRef<{ key: string; workspace: string } | null>(null);
  const mountedRef = useRef(false);
  const savingRef = useRef(false);
  const dirty = Boolean(title || content) && !savedDestination;
  useUnsavedChanges(loading || (dirty && draftStatus !== 'saved'), loading ? '笔记仍在保存，离开后请先核对知识库，避免重复保存。确定离开？' : undefined);

  useEffect(() => {
    mountedRef.current = true;
    const workspace = noteWorkspace(document.cookie);
    const scope = { workspace, key: noteDraftKey(workspace, 'new') };
    scopeRef.current = scope;
    const restored = readNoteDraft(browserDraftStorage(), scope.key);
    if (restored.draft && restored.draft.baseVersion === null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Hydrate browser-only tab storage after SSR, before enabling the form.
      setTitle(restored.draft.title);
      setContent(restored.draft.content);
      setRestoredAt(restored.savedAt);
      setDraftStatus('saved');
      setDraftExpiresAt(Number(restored.savedAt) + NOTE_DRAFT_TTL_MS);
    } else if (restored.status === 'unavailable') setDraftStatus('unavailable');
    else if (restored.status === 'expired') setNotice('此前暂存的草稿已超过 24 小时，未恢复。');
    else if (restored.status === 'invalid') setNotice('此前草稿格式无效，未恢复。');
    setReady(true);
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (draftStatus !== 'saved' || draftExpiresAt === null) return;
    const timer = window.setTimeout(() => setDraftStatus('expired'), Math.max(0, draftExpiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [draftStatus, draftExpiresAt]);

  function updateDraft(nextTitle: string, nextContent: string) {
    if (savingRef.current || savedDestination) return;
    setTitle(nextTitle);
    setContent(nextContent);
    const scope = scopeRef.current;
    if (!scope) return;
    const status = nextTitle || nextContent
      ? writeNoteDraft(browserDraftStorage(), scope.key, { title: nextTitle, content: nextContent, baseVersion: null })
      : clearNoteDraft(browserDraftStorage(), scope.key) ? 'idle' : 'unavailable';
    setDraftStatus(status);
    setDraftExpiresAt(status === 'saved' ? Date.now() + NOTE_DRAFT_TTL_MS : null);
  }

  function discardDraft() {
    if (!window.confirm('丢弃这份尚未保存的草稿？此操作不会删除知识库中的资料。')) return;
    const scope = scopeRef.current;
    if (!scope || !clearNoteDraft(browserDraftStorage(), scope.key)) {
      setDraftStatus('unavailable');
      setError('暂时无法清除浏览器草稿，正文仍保留，请稍后重试。');
      return;
    }
    setTitle(''); setContent(''); setRestoredAt(null); setNotice('草稿已丢弃。'); setDraftStatus('idle'); setError('');
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading || savingRef.current || savedDestination || !scopeRef.current) return;
    if (!content.trim()) { setError('请先填写正文内容'); return; }
    setLoading(true);
    savingRef.current = true;
    setError('');
    setLoginRequired(false);
    const scope = scopeRef.current;

    try {
      const res = await fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Cangzhi-Workspace': scope.workspace },
        body: JSON.stringify({ title, content, generate_title: !title.trim() }),
      });

      if (res.status === 401) {
        if (mountedRef.current) setLoginRequired(true);
        throw new Error(draftStatus === 'saved' ? '登录已过期。草稿已暂存，重新登录后可继续保存。' : '登录已过期。正文仍保留在此页，请先复制正文，再重新登录。');
      }
      if (!res.ok) throw new Error(apiErrorMessage(await res.json().catch(() => null), '保存失败，请重试'));
      const data = await res.json();
      if (!Number.isSafeInteger(data.id) || data.id <= 0) throw new Error('服务器未返回有效资料编号，请先到知识库核对，避免重复保存。');
      const cleared = clearNoteDraft(browserDraftStorage(), scope.key);
      if (!mountedRef.current) return;
      const destination = `/documents/${data.id}`;
      setSavedDestination(destination);
      if (!cleared) {
        setError('笔记已保存到知识库，但浏览器草稿未能清除。请勿重复保存；稍后重新进入时请丢弃旧草稿。');
        return;
      }
      runWithoutNavigationGuard(() => router.push(destination));
    } catch (reason) {
      if (mountedRef.current) setError(reason instanceof TypeError ? '无法连接服务器。正文仍保留在此页，请检查网络后重试。' : reason instanceof Error ? reason.message : '保存失败，请重试');
    } finally {
      savingRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  };

  return (
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>
      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">快速录入</p>
        <h1 className="mt-1 text-3xl font-semibold text-slate-950">记录一个想法</h1>
        <p className="mt-2 text-sm text-slate-500">先自由写下来，藏知会在后台完成整理、分类和索引。</p>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
        {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {loginRequired && <Link href="/login?next=%2Fnotes%2Fnew" className="inline-block text-sm font-medium text-blue-700 underline">重新登录后继续</Link>}
        {savedDestination && <Link href={savedDestination} className="inline-block text-sm font-medium text-blue-700 underline">查看已保存资料</Link>}
        {ready && !savedDestination && <NoteDraftStatus status={draftStatus} restoredAt={restoredAt} notice={notice} canDiscard={dirty && !loading} onDiscard={discardDraft} />}

        <div>
          <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-slate-700">
            标题 <span className="font-normal text-slate-400">可留空，自动取第一段</span>
          </label>
          <input
            id="title"
            type="text"
            maxLength={1024}
            disabled={loading || !ready || Boolean(savedDestination)}
            value={title}
            onChange={e => updateDraft(e.target.value, content)}
            className="w-full rounded-xl border px-3.5 py-2.5"
            placeholder="给这条记录起个名字"
          />
        </div>

        <div>
          <label htmlFor="content" className="mb-1.5 block text-sm font-medium text-slate-700">
            内容 <span className="font-normal text-slate-400">支持 Markdown</span>
          </label>
          <textarea
            id="content"
            disabled={loading || !ready || Boolean(savedDestination)}
            value={content}
            onChange={e => updateDraft(title, e.target.value)}
            className="min-h-[360px] w-full resize-y rounded-xl border px-4 py-3 leading-7"
            placeholder="此刻你在想什么？"
            required
          />
        </div>

        <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-5">
          <button
            type="submit"
            disabled={loading || !ready || !content.trim() || Boolean(savedDestination)}
            className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {loading ? '保存中...' : '保存'}
          </button>
          <Link
            href="/documents"
            className="rounded-xl border border-slate-300 px-5 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            取消
          </Link>
        </div>
      </form>
    </main>
  );
}
