import Link from 'next/link';

export default function Home() {
  return (
    <main className="mx-auto min-h-screen max-w-5xl px-6 py-16">
      <p className="mb-3 text-sm font-medium tracking-[0.2em] text-amber-700">CANGZHI</p>
      <h1 className="text-4xl font-semibold tracking-tight text-slate-900">藏知</h1>
      <p className="mt-4 max-w-xl text-lg leading-8 text-slate-600">
        先把资料和想法安心存下来，再通过搜索和带出处的 AI 回答随时找回。
      </p>
      <div className="mt-10 flex flex-wrap gap-3">
        <Link href="/ask" className="rounded-xl bg-slate-900 px-5 py-3 text-white">
          问知识库
        </Link>
        <Link href="/search" className="rounded-xl border border-slate-300 px-5 py-3">
          搜索资料
        </Link>
        <Link href="/notes/new" className="rounded-xl border border-slate-300 px-5 py-3">
          记录一个想法
        </Link>
        <Link href="/files/upload" className="rounded-xl border border-slate-300 px-5 py-3">
          上传资料
        </Link>
        <Link href="/links/new" className="rounded-xl border border-slate-300 px-5 py-3">
          收藏链接
        </Link>
        <Link href="/documents" className="rounded-xl border border-slate-300 px-5 py-3">
          查看全部资料
        </Link>
        <Link href="/categories" className="rounded-xl border border-slate-300 px-5 py-3">
          分类管理
        </Link>
      </div>
    </main>
  );
}
