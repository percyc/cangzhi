'use client';

import { useState } from 'react';

import { DatabaseSourcePanel } from '@/components/database-source-panel';
import { SettingsSectionNav } from '@/components/SettingsSectionNav';
import { WebDavSourcePanel } from '@/components/webdav-source-panel';

type SourceTab = 'file' | 'database';

const TABS: Array<{ id: SourceTab; title: string; hint: string }> = [
  { id: 'file', title: '文件来源', hint: 'WebDAV 连接、扫描与同步' },
  { id: 'database', title: '数据库来源', hint: '只读连接与导入表快照' },
];

export default function SourcesPage() {
  const [tab, setTab] = useState<SourceTab>('file');

  return (
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">系统设置</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">知识源</h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">
        连接外部文件夹与数据库，并统一管理扫描、同步及知识快照生命周期。
      </p>
      <SettingsSectionNav active="sources" />

      <div
        role="tablist"
        aria-label="知识源类型"
        className="mt-6 grid grid-cols-2 gap-2 rounded-2xl border border-slate-200 bg-white p-2"
      >
        {TABS.map((item) => {
          const selected = tab === item.id;
          return (
            <button
              key={item.id}
              role="tab"
              aria-selected={selected}
              onClick={() => setTab(item.id)}
              className={`rounded-xl p-3 text-left ${
                selected
                  ? 'bg-slate-950 text-white shadow-sm'
                  : 'text-slate-700 hover:bg-slate-50'
              }`}
            >
              <span className="block text-sm font-semibold">{item.title}</span>
              <span className={`mt-1 block text-xs ${selected ? 'text-slate-300' : 'text-slate-400'}`}>
                {item.hint}
              </span>
            </button>
          );
        })}
      </div>

      <div role="tabpanel">
        {tab === 'file' ? <WebDavSourcePanel /> : <DatabaseSourcePanel />}
      </div>
    </main>
  );
}
