'use client';

import Link from 'next/link';
import { useId, useRef, useState } from 'react';
import { SETTINGS_SEARCH_EXAMPLES, SETTINGS_SEARCH_MAX_QUERY, searchSettings, type SettingsSearchEntry } from '@/lib/settings-search';

export default function SettingsSearch({ beforeNavigate }: {
  beforeNavigate?: (entry: SettingsSearchEntry) => boolean;
}) {
  const [query, setQuery] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const links = useRef<(HTMLAnchorElement | null)[]>([]);
  const id = useId();
  const results = searchSettings(query);
  const searching = query.trim().length > 0;
  function clear() { setQuery(''); input.current?.focus(); }

  return <section className="min-w-0 rounded-2xl border border-slate-200 bg-white p-3" aria-label="设置与功能搜索">
    <label htmlFor={`${id}-query`} className="mb-2 block text-sm font-semibold text-slate-800">找设置或功能</label>
    <div className="relative">
      <input ref={input} id={`${id}-query`} type="search" value={query} maxLength={SETTINGS_SEARCH_MAX_QUERY}
        autoComplete="off" spellCheck={false} placeholder="如：上传上限、模型"
        aria-controls={`${id}-results`} aria-describedby={`${id}-help`}
        className="w-full min-w-0 rounded-xl border border-slate-300 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
        onChange={(event) => { setQuery(event.target.value); }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === 'ArrowDown' && results.length) { event.preventDefault(); links.current[0]?.focus(); }
          if (event.key === 'Enter' && results.length) { event.preventDefault(); links.current[0]?.click(); }
          if (event.key === 'Escape') { event.preventDefault(); clear(); }
        }} />
    </div>
    <p id={`${id}-help`} className="mt-2 text-[11px] leading-5 text-slate-500">只找入口，不读取资料或配置值，不调用 AI。</p>
    {!searching && <div className="mt-2 flex flex-wrap gap-1.5" aria-label="常用设置问法">
      {SETTINGS_SEARCH_EXAMPLES.map((example) => <button key={example} type="button" onClick={() => { setQuery(example); input.current?.focus(); }} className="rounded-lg bg-slate-100 px-2 py-1 text-xs text-slate-600 hover:bg-slate-200">{example}</button>)}
    </div>}
    <div id={`${id}-results`}>
      {searching && <>
        <div className="mt-3 flex items-center justify-between gap-2">
          <p className="text-xs text-slate-500" role="status">{results.length ? `相关入口 ${results.length} 项` : '没有匹配的入口'}</p>
          <button type="button" onClick={clear} className="rounded px-1 py-1 text-xs text-slate-600 underline underline-offset-2">清除</button>
        </div>
        {results.length ? <ul className="mt-1 space-y-1">
          {results.map((entry, index) => <li key={entry.id}>
            <Link ref={(element) => { links.current[index] = element; }} href={entry.href} prefetch={false}
              className="block rounded-xl border border-transparent px-2.5 py-2 hover:border-blue-100 hover:bg-blue-50 focus:bg-blue-50"
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                if (beforeNavigate && !beforeNavigate(entry)) event.preventDefault();
              }}
              onKeyDown={(event) => {
                if (event.key === 'ArrowDown') { event.preventDefault(); links.current[Math.min(index + 1, results.length - 1)]?.focus(); }
                if (event.key === 'ArrowUp') { event.preventDefault(); if (index === 0) input.current?.focus(); else links.current[index - 1]?.focus(); }
                if (event.key === 'Escape') { event.preventDefault(); clear(); }
              }}>
              <span className="block text-sm font-semibold text-slate-800">{entry.title}</span>
              <span className="mt-0.5 block break-words text-xs leading-5 text-slate-500">{entry.description}</span>
              <span className="mt-1 block text-[10px] text-slate-400">{entry.group}</span>
            </Link>
          </li>)}
        </ul> : <p className="mt-2 text-xs leading-5 text-slate-600">试试“密码”“向量”“WebDAV”等关键词，或从下方分类查找。若要查文档内容，请使用“搜资料”。</p>}
        {results.length > 0 && <p className="mt-2 text-[11px] leading-5 text-slate-400">Enter 打开首项，↓ 选择入口，Esc 清除。</p>}
      </>}
    </div>
  </section>;
}
