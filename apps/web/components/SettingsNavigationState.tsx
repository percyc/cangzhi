'use client';

import { createContext, useContext, useLayoutEffect, type Dispatch, type SetStateAction } from 'react';
import { usePathname } from 'next/navigation';
import type { SettingsSectionNavProps } from '@/components/SettingsSectionNav';

export type SettingsNavigationState = { pathname: string; props: SettingsSectionNavProps } | null;
// Only the mounted model form publishes hints and callbacks. The stable setter
// context does not rerender that form when the persistent navigation updates.
export const SettingsNavigationContext = createContext<Dispatch<SetStateAction<SettingsNavigationState>> | null>(null);

export function SettingsNavigationRegistration(props: SettingsSectionNavProps) {
  const publish = useContext(SettingsNavigationContext);
  const pathname = usePathname();
  useLayoutEffect(() => {
    const entry = { pathname, props };
    publish?.(entry);
    return () => publish?.(current => current === entry ? null : current);
  }, [publish, pathname, props]);
  return null;
}
