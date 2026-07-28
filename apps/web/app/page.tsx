import Link from 'next/link'

export default function Home() {
  return (
    <main className="mx-auto min-h-screen max-w-5xl px-6 py-16">
      <p className="mb-3 text-sm font-medium tracking-[0.2em] text-amber-700">CANGZHI</p>
      <h1 className="text-4xl font-semibold tracking-tight text-slate-900">藏知</h1>
      <p className="mt-4 max-w-xl text-lg leading-8 text-slate-600">
        先把资料和想法安心存下来。整理、理解和检索能力会在后续版本逐步补上。
      </p>
      <div className="mt-10 flex flex-wrap gap-3">
        <Link href="/notes/new" className="rounded-xl bg-slate-900 px-5 py-3 text-white">
          记录一个想法
        </Link>
        <Link href="/files/upload" className="rounded-xl border border-slate-300 px-5 py-3">
          上传资料
        </Link>
        <Link href="/documents" className="rounded-xl border border-slate-300 px-5 py-3">
          查看全部资料
        </Link>
      </div>
    </main>
  )
}
