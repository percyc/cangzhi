'use client';

import Link from 'next/link';

export type SettingsSection =
  | 'chat'
  | 'embedding'
  | 'sources'
  | 'data'
  | 'access';

type Item = {
  id: SettingsSection;
  href: string;
  title: string;
  defaultHint: string;
};

const ITEMS: Item[] = [
  {
    id: 'chat',
    href: '/settings?section=chat',
    title: '对话模型',
    defaultHint: '分类、摘要与知识问答',
  },
  {
    id: 'embedding',
    href: '/settings?section=embedding',
    title: '向量与索引',
    defaultHint: '语义检索与索引版本',
  },
  {
    id: 'sources',
    href: '/settings/sources',
    title: '知识源',
    defaultHint: 'WebDAV 连接与同步',
  },
  {
    id: 'data',
    href: '/settings/data',
    title: '数据与备份',
    defaultHint: '导出、备份与恢复',
  },
  {
    id: 'access',
    href: '/settings/access',
    title: '外部接入',
    defaultHint: 'CLI、Skill 与 MCP 令牌',
  },
];

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
  return (
    <nav
      aria-label="设置分类"
      className="mt-6 grid grid-cols-2 gap-2 rounded-2xl border border-slate-200 bg-white p-2 md:grid-cols-5"
    >
      {ITEMS.map((item) => {
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
            className={`min-w-0 rounded-xl p-3 text-left ${
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
                />
              )}
            </span>
            <span
              className={`mt-1 hidden truncate text-xs sm:block ${
                selected ? 'text-slate-300' : 'text-slate-400'
              }`}
            >
              {hints[item.id] || item.defaultHint}
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
