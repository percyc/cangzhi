'use client';

import { useEffect, useRef, useState } from 'react';
import { WEB_BUILD, buildLabel, compareBuilds, type BuildInfo } from '@/lib/build-info';
import { withApiBasePath } from '@/lib/paths';

export default function VersionInfo() {
  const [api, setApi] = useState<BuildInfo | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function load() {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    try {
      const response = await fetch(withApiBasePath('/api/system/version'), { cache: 'no-store', signal: request.signal });
      if (!response.ok) throw new Error(response.status === 401 ? '登录已失效，请重新登录后核对版本。' : 'API版本读取失败，可能仍运行旧版服务，请重试。');
      const body = await response.json();
      if (typeof body.version !== 'string' || typeof body.revision !== 'string' || typeof body.built_at !== 'string' || typeof body.dirty !== 'boolean') throw new Error('API未提供完整的构建信息。');
      if (!request.signal.aborted) setApi(body);
    } catch (caught) {
      if (!request.signal.aborted) { setApi(null); setError(caught instanceof Error ? caught.message : '无法核对API版本'); }
    } finally { if (!request.signal.aborted) setBusy(false); }
  }
  const consistency = api ? compareBuilds(WEB_BUILD, api) : 'unknown';
  return <details id="system-version" className="mt-4 min-w-0 scroll-mt-40 rounded-xl border border-slate-200 bg-white p-3 text-xs"
    onToggle={event => { if (event.currentTarget.open && !api && !busy) void load(); }}>
    <summary className="cursor-pointer break-words font-medium text-slate-600">版本信息 · {buildLabel(WEB_BUILD)}</summary>
    <div className="mt-3 space-y-3 break-words text-slate-600">
      <p>来自运行镜像的构建信息，不是服务器当前代码版本。</p>
      <BuildRow title="网页 Web" build={WEB_BUILD} />
      {api && <BuildRow title="服务 API" build={api} />}
      {busy ? <p role="status">正在核对API版本…</p> : error ? <p role="alert" className="text-amber-800">{error}</p> :
        <p role="status" className={consistency === 'mismatch' ? 'text-amber-800' : ''}>{consistency === 'match' ? 'Web/API版本一致' : consistency === 'mismatch' ? 'Web/API版本不一致，请确认各服务已完成更新并刷新页面。' : '构建信息不足或包含未提交修改，无法确认版本一致。'}</p>}
      <p>Worker版本可通过镜像标签或构建信息命令核验；此处不代表Worker运行状态。</p>
      <button type="button" disabled={busy} onClick={() => void load()} className="rounded-lg border px-3 py-2 disabled:opacity-50">重新核对</button>
    </div>
  </details>;
}

function BuildRow({ title, build }: { title: string; build: BuildInfo }) {
  return <div><p className="font-semibold">{title} · {build.version}</p>
    <p className="break-all">提交：{build.revision}</p><p>构建时间（UTC）：{build.built_at}</p>
    {build.dirty && <p className="text-amber-800">未验证的源码状态或包含未提交修改</p>}</div>;
}
