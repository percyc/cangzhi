'use client';

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { FormEvent, useEffect, useRef, useState } from 'react';
import { NoteDraftStatus } from '@/components/note-draft-status';
import { browserDraftStorage, clearNoteDraft, DraftWriteStatus, NoteDraft, NOTE_DRAFT_TTL_MS, noteDraftKey, noteSnapshot, noteWorkspace, readNoteDraft, sameNote, writeNoteDraft } from '@/lib/note-drafts';
import { runWithoutNavigationGuard, useUnsavedChanges } from '@/lib/navigation-guard';
import { apiErrorMessage } from '@/lib/usability';

export default function EditNotePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [baseVersion, setBaseVersion] = useState<number | null>(null);
  const [baseline, setBaseline] = useState<NoteDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [draftStatus, setDraftStatus] = useState<DraftWriteStatus>('idle');
  const [restoredAt, setRestoredAt] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [loginRequired, setLoginRequired] = useState(false);
  const [saved, setSaved] = useState(false);
  const [draftExpiresAt, setDraftExpiresAt] = useState<number | null>(null);
  const scopeRef = useRef<{ key: string; workspace: string; documentId: string } | null>(null);
  const mountedRef = useRef(false);
  const savingRef = useRef<symbol | null>(null);
  const current = { title, content, baseVersion };
  const dirty = !saved && (baseline ? !sameNote(current, baseline) : Boolean(title || content));
  useUnsavedChanges(saving || (dirty && draftStatus !== 'saved'), saving ? '笔记仍在保存，离开后请先核对资料，避免重复保存。确定离开？' : undefined);
  const loginHref = `/login?next=${encodeURIComponent(`/notes/${params.id}/edit`)}`;

  useEffect(() => {
    mountedRef.current = true;
    const workspace = noteWorkspace(document.cookie);
    const scope = { workspace, documentId: params.id, key: noteDraftKey(workspace, params.id) };
    scopeRef.current = scope;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Reset the old route and hydrate browser-only storage before enabling editing.
    setLoading(true); setBaseline(null); setError(''); setLoginRequired(false); setConflict(false); setSaved(false);
    savingRef.current = null; setSaving(false); setDraftExpiresAt(null);
    setTitle(''); setContent(''); setBaseVersion(null); setRestoredAt(null); setNotice(''); setDraftStatus('idle');
    const restored = readNoteDraft(browserDraftStorage(), scope.key);
    const draft = restored.draft?.baseVersion ? restored.draft : null;
    if (draft) {
      setTitle(draft.title); setContent(draft.content); setBaseVersion(draft.baseVersion);
      setRestoredAt(restored.savedAt); setDraftStatus('saved');
      setDraftExpiresAt(Number(restored.savedAt) + NOTE_DRAFT_TTL_MS);
    } else if (restored.status === 'unavailable') setDraftStatus('unavailable');
    else if (restored.status === 'expired') setNotice('此前暂存的草稿已超过 24 小时，未恢复。');
    else if (restored.status === 'invalid') setNotice('此前草稿格式无效，未恢复。');
    const active = () => mountedRef.current && scopeRef.current === scope && !controller.signal.aborted;
    void (async () => {
      try {
        const response = await fetch(`/api/documents/${scope.documentId}`, {
          cache: 'no-store', signal: controller.signal, headers: { 'X-Cangzhi-Workspace': scope.workspace },
        });
        if (response.status === 401) {
          if (active()) setLoginRequired(true);
          throw new Error('登录已过期，请重新登录后继续。现有草稿不会因此删除。');
        }
        if (!response.ok) throw new Error(apiErrorMessage(await response.json().catch(() => null), '无法读取这条随手记，请重试'));
        const original = noteSnapshot(await response.json());
        if (!active()) return;
        setBaseline(original);
        if (draft && !sameNote(draft, original)) {
          setConflict(draft.baseVersion !== original.baseVersion);
        } else {
          setTitle(original.title); setContent(original.content); setBaseVersion(original.baseVersion);
          if (draft) {
            const cleared = clearNoteDraft(browserDraftStorage(), scope.key);
            setDraftStatus(cleared ? 'idle' : 'unavailable'); setRestoredAt(null);
            setNotice('暂存内容已经存在于当前版本，无需再次保存。');
          }
        }
      } catch (reason) {
        if (active()) setError(reason instanceof Error ? reason.message : '无法读取这条随手记');
      } finally {
        if (active()) setLoading(false);
      }
    })();
    return () => { mountedRef.current = false; controller.abort(); };
  }, [params.id, loadAttempt]);

  useEffect(() => {
    if (draftStatus !== 'saved' || draftExpiresAt === null) return;
    const timer = window.setTimeout(() => setDraftStatus('expired'), Math.max(0, draftExpiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [draftStatus, draftExpiresAt]);

  function persist(next: NoteDraft) {
    const scope = scopeRef.current;
    if (!scope) return;
    const status = baseline && sameNote(next, baseline)
      ? clearNoteDraft(browserDraftStorage(), scope.key) ? 'idle' : 'unavailable'
      : writeNoteDraft(browserDraftStorage(), scope.key, next);
    setDraftStatus(status);
    setDraftExpiresAt(status === 'saved' ? Date.now() + NOTE_DRAFT_TTL_MS : null);
  }

  function updateDraft(nextTitle: string, nextContent: string) {
    if (savingRef.current || saved) return;
    setTitle(nextTitle); setContent(nextContent);
    persist({ title: nextTitle, content: nextContent, baseVersion });
  }

  function discardDraft() {
    if (!baseline || !window.confirm('丢弃尚未保存的草稿，恢复当前已保存的正文？')) return;
    const scope = scopeRef.current;
    if (!scope || !clearNoteDraft(browserDraftStorage(), scope.key)) {
      setDraftStatus('unavailable'); setError('暂时无法清除浏览器草稿，正文仍保留，请稍后重试。');
      return;
    }
    setTitle(baseline.title); setContent(baseline.content); setBaseVersion(baseline.baseVersion);
    setConflict(false); setRestoredAt(null); setDraftStatus('idle'); setNotice('草稿已丢弃，当前显示已保存版本。'); setError('');
  }

  function keepDraftAfterReview() {
    if (!baseline || !window.confirm('确认已核对最新正文，并保留本页草稿作为接下来要保存的内容？此时还不会修改知识库。')) return;
    setBaseVersion(baseline.baseVersion); setConflict(false);
    persist({ title, content, baseVersion: baseline.baseVersion });
    setNotice('已确认使用本页草稿。点击“保存修改”才会建立新版本。');
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    const scope = scopeRef.current;
    if (!scope || !baseline || loading || saving || savingRef.current || saved || conflict || !content.trim()) return;
    const attempt = Symbol('note-save');
    savingRef.current = attempt; setSaving(true); setError(''); setLoginRequired(false);
    const active = () => mountedRef.current && scopeRef.current === scope;
    const requireLogin = () => {
      if (active()) setLoginRequired(true);
      return new Error(draftStatus === 'saved' ? '登录已过期。草稿已暂存，重新登录后可继续保存。' : '登录已过期。正文仍保留在此页，请先复制正文，再重新登录。');
    };
    try {
      // The current API has no compare-and-swap field. Revalidate immediately
      // before PATCH and require visible confirmation for any observed change.
      const check = await fetch(`/api/documents/${scope.documentId}`, {
        cache: 'no-store', headers: { 'X-Cangzhi-Workspace': scope.workspace },
      });
      if (check.status === 401) throw requireLogin();
      if (!check.ok) throw new Error('保存前无法核对最新版本，未提交修改。请稍后重试。');
      const latest = noteSnapshot(await check.json());
      if (!active()) return;
      if (!sameNote(latest, baseline) || latest.baseVersion !== baseVersion) {
        setBaseline(latest); setConflict(true);
        setError('资料已在其他页面更新，本页草稿已保留。请先核对最新版本，再决定保存。');
        return;
      }
      const response = await fetch(`/api/notes/${scope.documentId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-Cangzhi-Workspace': scope.workspace },
        body: JSON.stringify({ title, content }),
      });
      if (response.status === 401) throw requireLogin();
      if (!response.ok) throw new Error(apiErrorMessage(await response.json().catch(() => null), '保存失败，请稍后重试'));
      const cleared = clearNoteDraft(browserDraftStorage(), scope.key);
      if (!active()) return;
      setSaved(true);
      if (!cleared) {
        setError('修改已保存，但浏览器草稿未能清除。请勿重复保存；稍后重新进入时请丢弃旧草稿。');
        return;
      }
      runWithoutNavigationGuard(() => router.push(`/documents/${scope.documentId}`));
    } catch (reason) {
      if (active()) setError(reason instanceof TypeError ? '无法连接服务器。正文仍保留在此页，请检查网络后重试。' : reason instanceof Error ? reason.message : '保存失败');
    } finally {
      if (savingRef.current === attempt) savingRef.current = null;
      if (active()) setSaving(false);
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href={`/documents/${params.id}`} className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回资料</Link>
      <h1 className="mb-2 mt-5 text-3xl font-semibold text-slate-950">编辑随手记</h1>
      <p className="mb-6 text-sm text-slate-500">修改后保存为新版本；草稿与已保存正文分开保留。</p>
      {loading ? <p role="status" className="rounded-xl border bg-white p-6 text-slate-600">正在核对已保存正文和本标签页草稿…</p> : !baseline ? (
        <section className="space-y-4 rounded-2xl border bg-white p-5">
          <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error || '正文未成功载入，暂不能编辑或保存。'}</p>
          {loginRequired && <Link href={loginHref} className="inline-block text-sm font-medium text-blue-700 underline">重新登录后继续</Link>}
          <button type="button" onClick={() => setLoadAttempt((value) => value + 1)} className="ml-3 rounded-lg border px-4 py-2 text-sm">重新读取</button>
          {(title || content) && <div>
            <p className="mb-2 text-sm text-slate-600">本标签页仍保留以下草稿。成功读取原文后才能编辑和保存：</p>
            <textarea aria-label="保留的草稿（只读）" readOnly className="min-h-56 w-full rounded-xl border bg-slate-50 p-3" value={`${title}\n\n${content}`} />
          </div>}
        </section>
      ) : (
        <form onSubmit={save} className="space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
          {error && <p role="alert" className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</p>}
          {loginRequired && <Link href={loginHref} className="inline-block text-sm font-medium text-blue-700 underline">重新登录后继续</Link>}
          {saved && <Link href={`/documents/${params.id}`} className="inline-block text-sm font-medium text-blue-700 underline">查看已保存资料</Link>}
          {!saved && <NoteDraftStatus status={draftStatus} restoredAt={restoredAt} notice={notice} canDiscard={dirty && !saving} onDiscard={discardDraft} />}
          {conflict && <section aria-label="版本变化提醒" className="space-y-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
            <p className="font-medium">已保存的原文有变化，草稿未被覆盖。请先核对，再决定是否使用本页草稿。</p>
            <details className="rounded-lg border border-amber-200 bg-white p-3">
              <summary className="cursor-pointer font-medium">查看最新已保存正文</summary>
              <h2 className="mt-3 font-medium">{baseline.title}</h2>
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words font-sans leading-6">{baseline.content.slice(0, 12000)}</pre>
              {baseline.content.length > 12000 && <p className="mt-2 text-xs">此处展示前 12,000 字符，可返回资料查看全文。</p>}
            </details>
            <div className="flex flex-wrap gap-3">
              <button type="button" onClick={keepDraftAfterReview} className="rounded-lg border border-amber-300 bg-white px-3 py-2 font-medium">已核对，继续使用草稿</button>
              <button type="button" onClick={discardDraft} className="px-3 py-2 underline">丢弃草稿，使用最新正文</button>
            </div>
          </section>}
          <div>
            <label htmlFor="note-title" className="mb-1.5 block text-sm font-medium text-slate-700">标题</label>
            <input id="note-title" aria-label="标题" className="w-full rounded-xl border px-3.5 py-2.5" maxLength={1024} disabled={saving || saved} onChange={(event) => updateDraft(event.target.value, content)} value={title} />
          </div>
          <div>
            <label htmlFor="note-content" className="mb-1.5 block text-sm font-medium text-slate-700">内容 <span className="font-normal text-slate-400">支持 Markdown</span></label>
            <textarea id="note-content" aria-label="内容" disabled={saving || saved} className="min-h-[360px] w-full resize-y rounded-xl border px-4 py-3 leading-7" onChange={(event) => updateDraft(title, event.target.value)} required value={content} />
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-5">
            <button className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white disabled:opacity-50" disabled={saving || saved || conflict || !content.trim() || !dirty} type="submit">{saving ? '保存中…' : '保存修改'}</button>
            <Link href={`/documents/${params.id}`} className="rounded-xl border px-5 py-2.5 text-sm text-slate-700">返回阅读</Link>
          </div>
        </form>
      )}
    </main>
  );
}
