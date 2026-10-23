'use client';

import { useState, type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import { SettingsSectionNav } from '@/components/SettingsSectionNav';
import { SettingsNavigationContext, type SettingsNavigationState } from '@/components/SettingsNavigationState';
import { SETTINGS_GROUPS } from '@/lib/usability';

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [navigation, setNavigation] = useState<SettingsNavigationState>(null);
  const active = SETTINGS_GROUPS.flatMap(group => [...group.items]).find(item => item.href === pathname)?.id ?? 'chat';
  // Never retain a departed form's callbacks/hints while a new route loads.
  const props = navigation?.pathname === pathname ? navigation.props : { active };
  return <SettingsNavigationContext.Provider value={setNavigation}>
    <div className="settings-layout mx-auto max-w-5xl px-5">
      <SettingsSectionNav {...props} />
      <div className="settings-content min-w-0">{children}</div>
    </div>
  </SettingsNavigationContext.Provider>;
}
