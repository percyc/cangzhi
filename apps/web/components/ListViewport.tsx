'use client';

import { useEffect, useRef, type ReactNode } from 'react';

export function ListPanel({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => element.style.setProperty('--panel-top', `${element.getBoundingClientRect().top + window.scrollY}px`);
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    window.addEventListener('resize', measure);
    measure();
    return () => { observer.disconnect(); window.removeEventListener('resize', measure); };
  }, []);
  return <section ref={ref} className="token-list-card rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">{children}</section>;
}

export function ListWorkspace({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      element.style.setProperty('--list-top', `${top}px`);
      element.dataset.fit = String(window.innerHeight - top >= 400);
    };
    const observer = new ResizeObserver(measure);
    // Header and expandable page guide are real layout inputs, not hard-coded heights.
    for (let sibling = element.previousElementSibling; sibling; sibling = sibling.previousElementSibling) observer.observe(sibling);
    window.addEventListener('resize', measure);
    measure();
    return () => { observer.disconnect(); window.removeEventListener('resize', measure); };
  }, []);
  return <main ref={ref} className="workspace-shell list-workspace">{children}</main>;
}

/** Only the results scroll on roomy desktops; mobile/short windows use the page. */
export function ListViewport({ children, resetKey, label }: { children: ReactNode; resetKey: string; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const previous = useRef(resetKey);
  useEffect(() => {
    if (previous.current !== resetKey && ref.current) {
      ref.current.scrollTop = 0;
      // In natural-flow mode return to the new results, not the bottom of the old page.
      if (!window.matchMedia('(min-width: 1024px) and (min-height: 700px)').matches) {
        ref.current.scrollIntoView({ block: 'start' });
      }
    }
    previous.current = resetKey;
  }, [resetKey]);
  return <div ref={ref} className="list-viewport" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}
