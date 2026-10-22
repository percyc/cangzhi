'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { SettingsSectionNav } from '@/components/SettingsSectionNav';
import { withApiBasePath } from '@/lib/paths';

type Metrics = Record<string, number | string | null>;
type MetricState = 'never' | 'first_failed' | 'degraded' | 'ok';
type Metric = { name: string; value: unknown; last_success_at: string | null;
  last_attempt_at: string | null; error: string | null; consecutive_failures: number;
  next_attempt_at: string | null; state: MetricState; stale?: boolean; attempt_age_seconds?: number | null };
type Section<T> = { data: T | null; sampled_at: string | null; attempted_at?: string | null;
  stale: boolean; error: string | null; metrics?: Metric[]; state?: string; interval_seconds?: number };
type Capacity = { disk: { total: number; used: number; free: number } | null; blob_bytes: number;
  original_bytes: number; preview_bytes: number; other_blob_bytes: number; parquet_bytes: number;
  database_bytes: number | null; vector_table_bytes: number | null; blobs: number };
type Pool = { pool_id: string; capacity: number; background_capacity: number; ocr_sublimit: number;
  in_use: number; background_in_use: number; cooldown_until: string | null };
type QueueMetrics = { waiting?: number; running?: number; failed?: number; oldest_waiting_at?: string;
  waiting_model?: number; quarantined?: number; recoveries?: number; model_pools?: Pool[] };
type Overview = { sections: { knowledge: Section<Metrics>; queue: Section<QueueMetrics>; capacity: Section<Capacity> };
  history: { at: string; documents: number | null; blob_bytes: number; disk_used: number | null }[] };

function bytes(value: number | null | undefined) {
  if (value == null) return '暂不可用';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const power = Math.min(4, Math.max(0, Math.floor(Math.log2(Math.max(1, value)) / 10)));
  return `${(value / 1024 ** power).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ${units[power]}`;
}
function count(value: number | string | null | undefined) {
  return value == null ? '—' : Number(value).toLocaleString('zh-CN');
}
function Stamp({ section }: { section?: Section<unknown> }) {
  if (!section) return null;
  const attempted = Boolean(section.attempted_at || section.error || section.state === 'first_failed');
  return <div className="mt-3 text-xs leading-5 text-slate-500">
    {section.sampled_at ? `最近成功指标采集于 ${new Date(section.sampled_at).toLocaleString('zh-CN')}` : attempted ? '已尝试采集，尚无可用结果' : '等待后台首次采集，尚无统计数据'}
    {section.stale && section.sampled_at && <span className="ml-2 text-amber-700">数据已过期；各指标时间见下方</span>}
    {section.error && !section.metrics?.length && <p className="text-amber-700">{section.sampled_at ? '本次采集暂不可用，显示上次成功结果。' : '首次采集失败，等待后台重试。'}</p>}
  </div>;
}
const metricLabels: Record<string, string> = {
  documents: '有效文档', files: '上传文件', trash: '回收站文档', versions: '内容版本',
  chunks: '在线检索切片', parent_chunks: '在线父片段', vectors: '全部存储向量',
  active_vectors: '当前有效向量', datasets: '可用数据集', waiting: '等待任务',
  running: '执行中任务', failed: '失败任务记录', oldest_waiting_at: '最早排队时间',
  model_pools: '模型共享额度', waiting_model: '等待模型额度/冷却', quarantined: '隔离任务', recoveries: '累计执行恢复',
  blobs: 'Blob 对象', blob_bytes: 'Blob 占用', original_bytes: '原文占用',
  preview_bytes: '预览占用', other_blob_bytes: '其他 Blob 占用', parquet_bytes: 'Parquet 占用',
  database_bytes: '数据库占用', vector_table_bytes: '向量表占用', disk: '数据盘容量',
};
function MetricHealth({ section }: { section?: Section<unknown> }) {
  if (!section) return null;
  const metrics = section.metrics || [];
  const failed = metrics.filter(m => m.state === 'degraded' || m.state === 'first_failed');
  const never = metrics.filter(m => m.state === 'never');
  const stale = metrics.filter(m => m.stale);
  const noRecentAttempt = metrics.some(m => (m.attempt_age_seconds || 0) > (section.interval_seconds || 900) * 3
    && (!m.next_attempt_at || (m.last_attempt_at && Date.parse(m.next_attempt_at)
      < Date.parse(m.last_attempt_at) + (m.attempt_age_seconds || 0) * 1000)));
  const issue = failed.length || never.length || stale.length;
  const summary = section.state === 'first_failed' ? '首次采集失败，尚无可用结果。'
    : section.state === 'partial' ? '部分统计可用；失败项保留旧值，无历史结果的项目显示未知。'
    : section.state === 'degraded' ? '本次采集失败，当前显示历史结果。'
    : stale.length ? '部分指标已过期，请以各自采集时间为准。' : '';
  const timeLabel = (time: string | null) => time ? new Date(time).toLocaleString('zh-CN') : '尚无';
  if (!metrics.length) return null;
  return <div className={`mt-2 rounded-lg p-3 text-xs leading-5 ${issue ? 'bg-amber-50 text-amber-900' : 'bg-slate-50 text-slate-500'}`}>
    {summary && <p>{summary}</p>}
    {noRecentAttempt && <p>长时间未收到采集尝试记录，请检查 Worker 与数据库连接。</p>}
    {failed.map(m => <p key={m.name}>
      {metricLabels[m.name] || m.name}：{m.error?.includes('超时') ? '采集超时或锁等待放弃' : '采集暂不可用'}；
      {m.last_success_at ? `保留 ${timeLabel(m.last_success_at)} 的旧值` : '尚无成功结果'}。
      {m.next_attempt_at && `预计 ${timeLabel(m.next_attempt_at)} 后重试。`}
    </p>)}
    {never.map(m => <p key={m.name}>{metricLabels[m.name] || m.name}：尚未采集，等待后台执行。</p>)}
    <details className="mt-1">
      <summary className="cursor-pointer">查看各指标采集时间（{metrics.length} 项）</summary>
      <div className="mt-2 max-h-60 space-y-2 overflow-auto">
        {metrics.map(m => <div key={m.name}>
          <p>{metricLabels[m.name] || m.name}{m.stale ? ' · 已过期' : ''}{m.state === 'degraded' ? ' · 旧值' : ''}</p>
          <p>最近成功：{timeLabel(m.last_success_at)} · 最近尝试：{timeLabel(m.last_attempt_at)}</p>
        </div>)}
      </div>
    </details>
  </div>;
}
const panel = 'min-w-0 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm';

export default function OverviewPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [days, setDays] = useState(7);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    async function load() {
      if (document.hidden) { timer = setTimeout(load, 30000); return; }
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(withApiBasePath(`/api/system/overview?days=${days}`), { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(response.status === 401 ? '登录已过期，请重新登录。' : '暂时无法读取统计快照，请稍后重试。');
        const result = await response.json();
        if (!disposed) { setData(result); setError(''); }
      } catch (err) {
        if (!disposed) setError(err instanceof Error && err.name !== 'AbortError' ? err.message : '读取超时，已保留之前的结果。');
      } finally {
        clearTimeout(timeout);
        if (!disposed) { setLoading(false); timer = setTimeout(load, 30000); }
      }
    }
    void load();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); };
  }, [days, revision]);
  const k = data?.sections.knowledge.data;
  const q = data?.sections.queue.data;
  const c = data?.sections.capacity.data;
  const queueSection = data?.sections.queue;
  const oldestKnown = queueSection?.metrics?.length
    ? Boolean(queueSection.metrics.find(m => m.name === 'oldest_waiting_at')?.last_success_at)
    : Boolean(q && queueSection?.sampled_at);
  const disk = c?.disk;
  const percent = disk && disk.total > 0 ? disk.used / disk.total * 100 : null;
  const history = data?.history || [];
  const maxBytes = Math.max(1, ...history.map(point => point.blob_bytes));
  // SVG breaks are not interpolated across unknown values; file bytes are always sampled.
  const startTime = history.length ? Date.parse(history[0].at) : 0;
  const timeSpan = history.length ? Math.max(1, Date.parse(history[history.length - 1].at) - startTime) : 1;
  const points = history.map(point => ({ x: (Date.parse(point.at) - startTime) * 600 / timeSpan, y: 100 - point.blob_bytes / maxBytes * 85, at: Date.parse(point.at) }));
  return <main className="settings-shell mx-auto max-w-6xl px-5 py-9">
    <p className="text-xs font-semibold tracking-widest text-slate-400">系统设置 / 全局监控</p>
    <h1 className="mt-2 text-3xl font-semibold text-slate-950">运行概览</h1>
    <p className="mt-2 text-sm leading-6 text-slate-500">观察容量、知识规模与处理负载。统计覆盖所有工作空间，不随顶部空间切换。</p>
    <SettingsSectionNav active="overview" />
    <div className="mt-6 space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500">
        <span>队列约每分钟采集 · 容量与规模约每 15 分钟采集 · 页面只读取快照</span>
        <button onClick={() => setRevision(v => v + 1)} className="rounded-lg border bg-white px-3 py-2 text-slate-700">重新读取快照</button>
      </div>
      {error && <p role="alert" className="rounded-xl bg-amber-50 p-4 text-sm text-amber-900">{error} {data && '以下为上次读取的数据。'}</p>}
      {loading && !data && <p role="status" className="text-sm text-slate-500">正在读取统计快照…</p>}
      {percent != null && percent >= 90 && <p role="alert" className="rounded-xl bg-rose-50 p-4 text-sm text-rose-900">数据盘已使用 {percent.toFixed(1)}%，请检查容量或扩容。不会自动删除资料。</p>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          ['数据盘可用', bytes(disk?.free), disk ? `已用 ${percent?.toFixed(1)}% · 整块挂载盘` : '磁盘统计暂不可用'],
          ['有效文档', count(k?.documents), `其中上传文件 ${count(k?.files)} 份`],
          ['在线检索切片', count(k?.chunks), '当前版本、未回收的子切片'],
          ['等待 / 执行中任务', `${count(q?.waiting)} / ${count(q?.running)}`, `失败任务记录 ${count(q?.failed)}`],
        ].map(([label, value, hint]) => <div key={label} className={panel}><p className="text-sm text-slate-500">{label}</p><p className="my-3 break-words text-2xl font-semibold tracking-tight text-slate-950">{value}</p><p className="text-xs leading-5 text-slate-500">{hint}</p></div>)}
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <section className={panel}>
          <h2 className="font-semibold text-slate-900">存储容量</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">数据盘使用率包含其他程序占用，不等于藏知占用。</p>
          <div className="my-4 h-3 overflow-hidden rounded-full bg-slate-100" role={percent == null ? undefined : 'meter'} aria-label="数据盘使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent == null ? undefined : Math.round(percent)}>
            {percent != null && <div className={`h-full rounded-full ${percent >= 90 ? 'bg-rose-500' : 'bg-teal-600'}`} style={{ width: `${Math.min(100, percent)}%` }} />}
          </div>
          <p className="mb-5 text-sm text-slate-600">已用 {bytes(disk?.used)} / 总量 {bytes(disk?.total)}</p>
          <dl className="space-y-3 text-sm">
            {[
              ['已登记原文件 / 网页快照', c?.original_bytes], ['PDF 等预览（不重复计原件）', c?.preview_bytes],
              ['其他已登记 Blob', c?.other_blob_bytes], ['已就绪 Parquet 产物', c?.parquet_bytes],
              ['数据库（含表、索引）', c?.database_bytes], ['其中：向量表及其索引', c?.vector_table_bytes],
            ].map(([label, value]) => <div key={String(label)} className="flex justify-between gap-4"><dt className="text-slate-500">{label}</dt><dd className="shrink-0 font-medium tabular-nums">{bytes(value as number | null | undefined)}</dd></div>)}
          </dl>
          <p className="mt-4 text-xs leading-5 text-slate-500">文件占用按数据库登记字节去重汇总，不扫描目录；含历史版本与回收站。不含未登记临时文件、备份及 Docker 镜像。数据库可能位于另一块盘，以上数字不可直接相加解释数据盘占比。</p>
          <Stamp section={data?.sections.capacity} />
          <MetricHealth section={data?.sections.capacity} />
        </section>
        <section className={panel}>
          <h2 className="font-semibold text-slate-900">知识资产</h2>
          <dl className="mt-5 grid grid-cols-2 gap-5">
            {[
              ['有效文档', k?.documents], ['回收站文档', k?.trash], ['全部内容版本', k?.versions],
              ['在线父片段', k?.parent_chunks], ['可用数据集', k?.datasets], ['去重 Blob 对象', c?.blobs],
              ['当前有效向量', k?.active_vectors], ['全部存储向量', k?.vectors],
            ].map(([label, value]) => <div key={String(label)}><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{count(value)}</dd></div>)}
          </dl>
          <p className="mt-5 text-xs leading-5 text-slate-500">文档包含文件、网页与随手记。全部向量含候选和旧 Profile；当前有效向量匹配生效 Profile 与切片内容哈希。数据集可能只生成抽样向量，数量不代表覆盖率或检索质量。</p>
          <Stamp section={data?.sections.knowledge} />
          <MetricHealth section={data?.sections.knowledge} />
          <Link href="/settings?section=embedding" className="mt-4 inline-block text-sm text-teal-700">查看向量构建与覆盖 →</Link>
        </section>
      </div>
      <section className={panel}>
        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold text-slate-900">容量与知识增长</h2><select aria-label="趋势时间范围" value={days} onChange={e => setDays(Number(e.target.value))} className="rounded-lg border px-3 py-2 text-sm"><option value={7}>最近 7 天</option><option value={30}>最近 30 天</option></select></div>
        <p className="mt-2 text-xs text-slate-500">每小时保留一个观测点，从启用监控后开始积累，不补造历史。</p>
        {history.length < 2 ? <p className="py-9 text-center text-sm text-slate-500">趋势正在积累，至少两个小时观测点后显示曲线。</p> : <>
          <div className="mt-5 flex flex-wrap justify-between gap-2 text-sm"><span className="text-teal-700">已登记 Blob 字节 · {bytes(history[0].blob_bytes)} → {bytes(history[history.length - 1].blob_bytes)}</span><span>有效文档 {count(history[0].documents)} → {count(history[history.length - 1].documents)}</span></div>
          <svg viewBox="-3 0 606 110" role="img" aria-label="已登记文件容量趋势" className="my-4 w-full">{points.map((p, i) => <g key={p.at}><circle cx={p.x} cy={p.y} r="2" fill="#0d9488" />{i > 0 && p.at - points[i-1].at <= 5400000 && <line x1={points[i-1].x} y1={points[i-1].y} x2={p.x} y2={p.y} stroke="#0d9488" strokeWidth="2" vectorEffect="non-scaling-stroke" />}</g>)}</svg>
          <div className="flex justify-between text-xs text-slate-500"><span>{new Date(history[0].at).toLocaleString('zh-CN')}</span><span>{new Date(history[history.length - 1].at).toLocaleString('zh-CN')}</span></div>
          <details className="mt-4 text-sm"><summary className="cursor-pointer text-slate-600">查看最近 24 个观测点</summary><div className="mt-3 max-h-64 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="py-2">时间</th><th>文档</th><th>Blob 占用</th></tr></thead><tbody>{history.slice(-24).reverse().map(point => <tr key={point.at} className="border-t"><td className="py-2">{new Date(point.at).toLocaleString('zh-CN')}</td><td>{count(point.documents)}</td><td>{bytes(point.blob_bytes)}</td></tr>)}</tbody></table></div></details>
        </>}
      </section>
      <section className={panel}>
        <h2 className="font-semibold text-slate-900">处理队列</h2>
        <p className="mt-2 text-sm leading-6 text-slate-600">等待 {count(q?.waiting)} · 执行中 {count(q?.running)} · 失败记录 {count(q?.failed)}</p>
        <p className="mt-2 text-sm text-slate-600">等待模型额度/渠道恢复 {count(q?.waiting_model)} · 隔离待检查 {count(q?.quarantined)} · 累计恢复 {count(q?.recoveries)}</p>
        <div className="mt-3 space-y-2 text-sm text-slate-600">
          {q?.model_pools?.map(pool => <div key={pool.pool_id} className="rounded-lg bg-slate-50 p-3">
            <p>额度池 {pool.pool_id}：使用 {pool.in_use} / {pool.capacity}；后台 {pool.background_in_use} / {pool.background_capacity}；OCR 上限 {pool.ocr_sublimit}</p>
            {pool.cooldown_until && <p>最近记录的冷却截止：{new Date(pool.cooldown_until).toLocaleString('zh-CN')}</p>}
          </div>)}
          <p className="text-xs">额度占用为后台采样快照，不是实时计数。默认共享额度池，后台不会占满前台保留名额。</p>
        </div>
        <p className="mt-2 text-sm text-slate-600">最早排队时间：{oldestKnown ? q?.oldest_waiting_at ? new Date(String(q.oldest_waiting_at)).toLocaleString('zh-CN') : '暂无排队任务' : '暂不可用'}</p>
        <p className="mt-2 text-xs leading-5 text-slate-500">统计当前、未回收文档的任务记录，不是文档数；一篇文档可产生多个向量任务。失败记录可能包含已重试的旧尝试，不能直接当作当前失败文档数或成功率。WebDAV 扫描任务另见知识源。</p>
        <Stamp section={data?.sections.queue} />
        <MetricHealth section={data?.sections.queue} />
        <div className="mt-4 flex flex-wrap gap-5 text-sm text-teal-700"><Link href="/processing">查看处理任务 →</Link><Link href="/inbox">处理待办与失败 →</Link><Link href="/settings/sources">查看来源扫描 →</Link></div>
      </section>
      <p className="text-xs leading-5 text-slate-500">本页不调用模型、不改变索引。检索 P95、调用成功率和备份完成时间尚未接入，不以数据库探活耗时冒充查询性能。</p>
    </div>
  </main>;
}
