'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { SETTINGS_GROUPS, type SettingsSection } from '@/lib/usability';

export type { SettingsSection } from '@/lib/usability';

// Retain the exported section contract; grouping is shared with route help/tests.
const ITEMS = SETTINGS_GROUPS.flatMap((group) => [...group.items]);

export function SettingsSectionNav({
  active,
  hints = {},
  dirty = {},
  onSelect,
  beforeNavigate,
}: {
  active: SettingsSection;
  hints?: Partial<Record<SettingsSection, string>>;
  dirty?: Partial<Record<SettingsSection, boolean>>;
  onSelect?: (section: SettingsSection) => void;
  beforeNavigate?: (section: SettingsSection) => boolean;
}) {
  const router = useRouter();
  return (
    <aside className="settings-rail mt-6" aria-label="设置导航">
      <div className="rounded-2xl border border-slate-200 bg-white p-3 lg:hidden">
        <label htmlFor="settings-section" className="mb-2 block text-xs font-medium text-slate-500">切换设置页面</label>
        <select id="settings-section" value={active} className="w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"
          onChange={(event) => {
            const item = ITEMS.find((entry) => entry.id === event.target.value);
            if (!item || item.id === active || (beforeNavigate && !beforeNavigate(item.id))) return;
            onSelect?.(item.id);
            router.push(item.href);
          }}>
          {SETTINGS_GROUPS.map((group) => <optgroup key={group.title} label={group.title}>{group.items.map((item) => <option key={item.id} value={item.id}>{item.title}{dirty[item.id] ? ' · 未保存' : ''}</option>)}</optgroup>)}
        </select>
      </div>
    <nav
      aria-label="设置分类"
      className="settings-rail-inner hidden space-y-5 rounded-2xl border border-slate-200 bg-white p-3 lg:block"
    >
      {SETTINGS_GROUPS.map((group) => <div key={group.title}>
        <p className="mb-2 px-3 text-[11px] font-semibold text-slate-400">{group.title}</p>
        <div className="space-y-1">{group.items.map((item) => {
        const selected = active === item.id;
        return (
          <Link
            key={item.id}
            href={item.href}
            aria-current={selected ? 'page' : undefined}
            onClick={(event) => {
              if (beforeNavigate && !beforeNavigate(item.id)) {
                event.preventDefault();
                return;
              }
              onSelect?.(item.id);
            }}
            className={`block min-w-0 rounded-xl px-3 py-2.5 text-left ${
              selected
                ? 'bg-slate-950 text-white shadow-sm'
                : 'text-slate-700 hover:bg-slate-50'
            }`}
          >
            <span className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-semibold">{item.title}</span>
              {dirty[item.id] && (
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${
                    selected ? 'bg-amber-300' : 'bg-amber-500'
                  }`}
                  title="有未保存的修改"
                  aria-label="有未保存的修改"
                />
              )}
            </span>
            <span
              className={`mt-1 block break-words text-xs leading-5 ${
                selected ? 'text-slate-300' : 'text-slate-500'
              }`}
            >
              {hints[item.id] || item.hint}
            </span>
          </Link>
        );
      })}</div></div>)}
    </nav>
    </aside>
  );
}
