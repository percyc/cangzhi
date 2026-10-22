'use client';

import Link from 'next/link';
import { useId, useState } from 'react';
import { useRouter } from 'next/navigation';
import { SETTINGS_GROUPS, type SettingsSection } from '@/lib/usability';
import VersionInfo from '@/components/VersionInfo';
import SettingsSearch from '@/components/SettingsSearch';
import { confirmNavigation } from '@/lib/navigation-guard';

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
  const [expanded, setExpanded] = useState(false);
  const navigationId = useId();
  return (
    <aside className="settings-rail mt-6" aria-label="设置导航">
      <div className="settings-rail-inner">
      <button type="button" aria-expanded={expanded} aria-controls={navigationId}
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-sm font-medium text-slate-700 lg:hidden">
        <span>查找或切换设置</span><span aria-hidden="true" className="text-xs text-slate-500">{expanded ? '收起 ↑' : '展开 ↓'}</span>
      </button>
      <div id={navigationId} className={`settings-rail-links space-y-3 ${expanded ? 'mt-3' : 'hidden'} lg:mt-0 lg:block`}>
      <SettingsSearch beforeNavigate={(entry) => {
        if (entry.section) {
          if (beforeNavigate && !beforeNavigate(entry.section)) return false;
          onSelect?.(entry.section);
          return true;
        }
        return confirmNavigation();
      }} />
      <div className="rounded-2xl border border-slate-200 bg-white p-3 lg:hidden">
        <label htmlFor="settings-section" className="mb-2 block text-xs font-medium text-slate-500">切换设置页面</label>
        <select id="settings-section" value={active} className="w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"
          onChange={(event) => {
            const item = ITEMS.find((entry) => entry.id === event.target.value);
            if (!item || item.id === active || !(beforeNavigate ? beforeNavigate(item.id) : confirmNavigation())) return;
            onSelect?.(item.id);
            router.push(item.href);
          }}>
          {SETTINGS_GROUPS.map((group) => <optgroup key={group.title} label={group.title}>{group.items.map((item) => <option key={item.id} value={item.id}>{item.title}{dirty[item.id] ? ' · 未保存' : ''}</option>)}</optgroup>)}
        </select>
      </div>
    <nav
      aria-label="设置分类"
      className="hidden space-y-5 rounded-2xl border border-slate-200 bg-white p-3 lg:block"
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
      </div>
    <VersionInfo />
      </div>
    </aside>
  );
}
