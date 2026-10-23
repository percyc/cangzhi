export default function Loading() {
  return <main className="settings-shell py-9" aria-busy="true">
    <p role="status" className="text-sm text-slate-500">正在打开设置页面…</p>
    <div aria-hidden="true" className="mt-6 h-64 rounded-2xl border border-slate-200 bg-white" />
  </main>;
}
