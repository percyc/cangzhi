'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { pageHelp, type PageHelp } from '@/lib/usability';

export function GuideContent({ help }: { help: PageHelp }) {
  return (
    <div className="grid gap-4 border-t border-slate-200/70 px-4 py-4 sm:grid-cols-[1fr_18rem]">
      <ol className="grid gap-2 text-sm leading-6 text-slate-600">
        {help.steps.map((step, index) => <li key={step} className="flex gap-3"><span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-white text-xs font-semibold text-slate-500">{index + 1}</span><span>{step}</span></li>)}
      </ol>
      <div><p className="text-xs leading-6 text-slate-500">{help.note}</p><Link href={help.action.href} className="mt-2 inline-flex text-sm font-medium text-slate-800 underline underline-offset-4">{help.action.label} →</Link></div>
    </div>
  );
}

export function PageGuide() {
  const pathname = usePathname() ?? '/';
  const search = useSearchParams();
  const help = pageHelp(pathname, search?.get('section') ?? 'chat');
  if (!help || pathname === '/ask') return null;
  return (
    <div className="mx-auto w-full max-w-7xl px-4 pt-3 sm:px-6">
      <details key={`${pathname}:${search?.get('section')}`} className="group rounded-xl border border-slate-200/70 bg-slate-50/80">
        <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 rounded-xl px-4 py-2.5 text-xs text-slate-500">
          <span className="font-semibold text-slate-700">如何使用此页</span><span className="flex-1">{help.title}</span><span aria-hidden="true" className="group-open:hidden">展开 ↓</span><span aria-hidden="true" className="hidden group-open:inline">收起 ↑</span>
        </summary>
        <GuideContent help={help} />
      </details>
    </div>
  );
}
