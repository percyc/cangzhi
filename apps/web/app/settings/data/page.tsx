'use client';

import { useState } from 'react';

import { SettingsSectionNav } from '@/components/SettingsSectionNav';

export default function DataSettingsPage() {
  const [includeTrashed, setIncludeTrashed] = useState(false);
  const [includeOriginals, setIncludeOriginals] = useState(false);

  const query = new URLSearchParams({
    include_trashed: String(includeTrashed),
    include_originals: String(includeOriginals),
  });

  return (
    <main className="mx-auto max-w-5xl px-4 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">设置</h1>
      <p className="mt-2 text-sm text-slate-500">
        管理模型、知识来源和系统处理能力。日常整理请前往知识库或收件箱。
      </p>
      <SettingsSectionNav active="data" />

      <div className="mt-8">
        <p className="text-xs font-semibold uppercase tracking-wide text-violet-600">
          数据主权
        </p>
        <h2 className="mt-1 text-xl font-semibold text-slate-900">
          数据与备份
        </h2>
        <p className="mt-1 text-sm leading-6 text-slate-500">
          导出包不依赖藏知即可阅读。Markdown 用于日常迁移，JSON 保留完整结构和处理元数据。
        </p>
      </div>

      <section className="mt-6 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h3 className="font-semibold text-slate-900">导出整个知识库</h3>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">
              ZIP 中包含每条知识的可读 Markdown、结构化 JSON、文件清单，以及按需附带的原文件。
            </p>
          </div>
          <a
            href={`/api/exports/library?${query.toString()}`}
            className="rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-800"
          >
            下载导出包
          </a>
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="flex cursor-pointer gap-3 rounded-xl border border-slate-200 p-4">
            <input
              type="checkbox"
              checked={includeTrashed}
              onChange={(event) => setIncludeTrashed(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              <span className="block text-sm font-medium text-slate-800">
                包含回收站
              </span>
              <span className="mt-1 block text-xs leading-5 text-slate-500">
                同时导出尚未永久删除的资料，并在清单中标注删除状态。
              </span>
            </span>
          </label>
          <label className="flex cursor-pointer gap-3 rounded-xl border border-slate-200 p-4">
            <input
              type="checkbox"
              checked={includeOriginals}
              onChange={(event) => setIncludeOriginals(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              <span className="block text-sm font-medium text-slate-800">
                包含本地原文件
              </span>
              <span className="mt-1 block text-xs leading-5 text-slate-500">
                包体会明显变大；仅附带藏知本地仍保存的原文，不读取或修改 WebDAV 远端。
              </span>
            </span>
          </label>
        </div>
      </section>

      <section className="mt-4 rounded-2xl border border-amber-200 bg-amber-50/60 p-5">
        <h3 className="font-semibold text-amber-950">导出不等于完整系统备份</h3>
        <p className="mt-2 text-sm leading-6 text-amber-900/80">
          导出适合阅读和迁移知识内容；完整恢复还需要数据库、storage 目录及加密主密钥。自动备份与恢复校验将在本阶段下一批交付。
        </p>
      </section>
    </main>
  );
}
