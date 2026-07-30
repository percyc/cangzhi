'use client';

import Link from 'next/link';
import { FormEvent, useCallback, useEffect, useState } from 'react';

type Tag = {
  id: number;
  slug: string;
  name: string;
  description: string | null;
};
type Suggestion = {
  target_tag_id: number;
  source_tag_ids: number[];
  reason: string;
  confidence: number;
};
type MergeHistory = {
  id: number;
  status: string;
  source: { name: string };
  target: { id: number; name: string } | null;
  reason: string | null;
};

export default function TagsPage() {
  const [tags, setTags] = useState<Tag[]>([]);
  const [newName, setNewName] = useState('');
  const [mergeSourceId, setMergeSourceId] = useState('');
  const [mergeTargetId, setMergeTargetId] = useState('');
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [selectedSuggestions, setSelectedSuggestions] = useState<number[]>([]);
  const [history, setHistory] = useState<MergeHistory[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    const [tagsResponse, historyResponse] = await Promise.all([
      fetch('/api/tags', { cache: 'no-store' }),
      fetch('/api/tags/merges/history', { cache: 'no-store' }),
    ]);
    if (!tagsResponse.ok || !historyResponse.ok) throw new Error('标签读取失败');
    setTags((await tagsResponse.json()) as Tag[]);
    setHistory((await historyResponse.json()) as MergeHistory[]);
    setLoading(false);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load().catch((caught) => {
        setError(caught instanceof Error ? caught.message : '标签读取失败');
        setLoading(false);
      });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!newName.trim()) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/tags', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName.trim() }),
      });
      if (!response.ok) throw new Error('创建标签失败');
      setNewName('');
      setMessage('标签已创建');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '创建标签失败');
    } finally {
      setBusy(false);
    }
  };

  const rename = async (tag: Tag) => {
    const name = window.prompt('新的标签名称', tag.name)?.trim();
    if (!name || name === tag.name) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/tags/${tag.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!response.ok) throw new Error('重命名失败');
      setMessage(`“${tag.name}”已重命名为“${name}”`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '重命名失败');
    } finally {
      setBusy(false);
    }
  };

  const merge = async () => {
    if (!mergeSourceId || !mergeTargetId || mergeSourceId === mergeTargetId) {
      setError('请选择两个不同的标签');
      return;
    }
    const source = tags.find((tag) => tag.id === Number(mergeSourceId));
    const target = tags.find((tag) => tag.id === Number(mergeTargetId));
    if (!source || !target) return;
    if (!window.confirm(`将“${source.name}”合并到“${target.name}”吗？所有资料关联会迁移到目标标签。`)) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch(`/api/tags/${source.id}/merge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_tag_id: target.id }),
      });
      if (!response.ok) throw new Error('合并标签失败');
      setMergeSourceId('');
      setMergeTargetId('');
      setMessage(`已将“${source.name}”合并到“${target.name}”`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '合并标签失败');
    } finally {
      setBusy(false);
    }
  };

  const analyze = async () => {
    setAnalyzing(true);
    setError('');
    setMessage('');
    try {
      const response = await fetch('/api/tags/suggestions', { method: 'POST' });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail || 'AI 标签分析失败');
      }
      const body = (await response.json()) as { groups: Suggestion[] };
      setSuggestions(body.groups);
      setSelectedSuggestions(body.groups.map((_item, index) => index));
      setMessage(
        body.groups.length
          ? `AI 找到 ${body.groups.length} 组合并建议，请确认后执行。`
          : 'AI 没有发现足够可靠的合并建议。',
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'AI 标签分析失败');
    } finally {
      setAnalyzing(false);
    }
  };

  const applySuggestions = async () => {
    const groups = suggestions.filter((_item, index) =>
      selectedSuggestions.includes(index),
    );
    if (!groups.length) return;
    if (!window.confirm(`确定执行选中的 ${groups.length} 组合并建议吗？合并后可以从历史记录撤销。`)) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/tags/merge-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ groups }),
      });
      if (!response.ok) throw new Error('批量合并失败');
      setSuggestions([]);
      setSelectedSuggestions([]);
      setMessage('已完成 AI 建议的标签合并，可在合并历史中撤销。');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量合并失败');
    } finally {
      setBusy(false);
    }
  };

  const undo = async (record: MergeHistory) => {
    if (!window.confirm(`撤销“${record.source.name}”的合并吗？`)) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/tags/merges/${record.id}/undo`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error('撤销合并失败');
      setMessage('标签合并已撤销');
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '撤销合并失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="container mx-auto max-w-5xl p-4">
      <Link href="/documents" className="text-blue-600 hover:underline">← 返回知识库</Link>
      <h1 className="mt-4 text-2xl font-bold">标签管理</h1>
      <p className="mt-2 text-sm text-slate-500">
        标签用于跨分类描述主题。重复或含义相近的标签建议合并，避免越积越乱。
      </p>

      {message && <p className="mt-4 rounded bg-emerald-50 p-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="mt-4 rounded bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      <div className="mt-6 grid gap-4 md:grid-cols-2">
        <form onSubmit={create} className="rounded-xl border bg-white p-4">
          <h2 className="font-semibold">新建标签</h2>
          <div className="mt-3 flex gap-2">
            <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="标签名称" className="min-w-0 flex-1 rounded border border-slate-300 px-3 py-2 text-sm" />
            <button disabled={busy || !newName.trim()} className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40">创建</button>
          </div>
        </form>
        <section className="rounded-xl border bg-white p-4">
          <h2 className="font-semibold">合并重复标签</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <select value={mergeSourceId} onChange={(event) => setMergeSourceId(event.target.value)} className="min-w-32 flex-1 rounded border border-slate-300 px-2 py-2 text-sm">
              <option value="">需要合并的标签</option>
              {tags.map((tag) => <option key={tag.id} value={tag.id}>{tag.name}</option>)}
            </select>
            <span className="self-center text-sm text-slate-400">合并到</span>
            <select value={mergeTargetId} onChange={(event) => setMergeTargetId(event.target.value)} className="min-w-32 flex-1 rounded border border-slate-300 px-2 py-2 text-sm">
              <option value="">保留的目标标签</option>
              {tags.map((tag) => <option key={tag.id} value={tag.id}>{tag.name}</option>)}
            </select>
            <button type="button" disabled={busy} onClick={merge} className="rounded border border-slate-300 px-3 py-2 text-sm disabled:opacity-40">合并</button>
          </div>
        </section>
      </div>

      <section className="mt-6 rounded-xl border border-violet-200 bg-violet-50/40 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-900">AI 标签治理建议</h2>
            <p className="mt-1 text-sm text-slate-500">AI 只给出保守建议，不会自动修改；确认合并后仍可撤销。</p>
          </div>
          <button type="button" disabled={analyzing || busy || tags.length < 2} onClick={analyze} className="rounded bg-violet-700 px-3 py-2 text-sm text-white disabled:opacity-40">
            {analyzing ? '分析中…' : '让 AI 检查重复标签'}
          </button>
        </div>
        {suggestions.length > 0 && (
          <div className="mt-4 space-y-2">
            {suggestions.map((suggestion, index) => {
              const target = tags.find((tag) => tag.id === suggestion.target_tag_id);
              const sources = suggestion.source_tag_ids
                .map((id) => tags.find((tag) => tag.id === id)?.name)
                .filter(Boolean);
              return (
                <label key={`${suggestion.target_tag_id}-${index}`} className="flex gap-3 rounded-lg border border-violet-100 bg-white p-3">
                  <input type="checkbox" checked={selectedSuggestions.includes(index)} onChange={(event) => setSelectedSuggestions((current) => event.target.checked ? [...current, index] : current.filter((item) => item !== index))} />
                  <span className="text-sm">
                    <span className="font-medium">{sources.join('、')} → {target?.name}</span>
                    <span className="ml-2 text-xs text-violet-700">置信度 {Math.round(suggestion.confidence * 100)}%</span>
                    <span className="mt-1 block text-xs text-slate-500">{suggestion.reason}</span>
                  </span>
                </label>
              );
            })}
            <button type="button" disabled={busy || !selectedSuggestions.length} onClick={applySuggestions} className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40">合并选中建议</button>
          </div>
        )}
      </section>

      {loading ? (
        <p className="mt-6 text-slate-500">加载中…</p>
      ) : (
        <div className="mt-6 rounded-xl border bg-white">
          {tags.map((tag) => (
            <div key={tag.id} className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3 last:border-0">
              <span className="text-sm text-slate-800">#{tag.name}</span>
              <button type="button" disabled={busy} onClick={() => void rename(tag)} className="rounded px-2 py-1 text-xs text-blue-700 hover:bg-blue-50 disabled:opacity-40">重命名</button>
            </div>
          ))}
          {!tags.length && <p className="p-8 text-center text-sm text-slate-500">还没有标签。</p>}
        </div>
      )}

      {history.length > 0 && (
        <section className="mt-6 rounded-xl border bg-white p-4">
          <h2 className="font-semibold">合并历史</h2>
          <div className="mt-3 space-y-2">
            {history.slice(0, 20).map((record) => (
              <div key={record.id} className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-2 text-sm first:border-0">
                <span>
                  {record.source.name} → {record.target?.name ?? '目标标签已删除'}
                  {record.reason && <span className="ml-2 text-xs text-slate-400">{record.reason}</span>}
                </span>
                {record.status === 'active' ? (
                  <button type="button" disabled={busy} onClick={() => void undo(record)} className="rounded px-2 py-1 text-xs text-amber-700 hover:bg-amber-50">撤销</button>
                ) : (
                  <span className="text-xs text-slate-400">已撤销</span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
