'use client';

import { useEffect } from 'react';

const guards = new Map<symbol, string>();
let bypassDepth = 0;
const POSITION = '__cangzhiNavigationPosition';

export function registerUnsavedChanges(message: string): () => void {
  const id = Symbol('unsaved');
  guards.set(id, message);
  return () => { guards.delete(id); };
}

export function useUnsavedChanges(enabled: boolean, message = '此页有未保存的修改，离开后将丢失。确定离开吗？') {
  useEffect(() => enabled ? registerUnsavedChanges(message) : undefined, [enabled, message]);
}

export function confirmNavigation(): boolean {
  return bypassDepth > 0 || guards.size === 0 || window.confirm([...new Set(guards.values())].join('\n'));
}

// Only bypass the current, explicitly approved navigation. Never disable the
// guard for a future edit or silently persist a credential-bearing form.
export function runWithoutNavigationGuard<T>(action: () => T): T {
  bypassDepth += 1;
  try { return action(); } finally {
    queueMicrotask(() => { bypassDepth -= 1; });
  }
}

export function installNavigationGuards(): () => void {
  const beforeUnload = (event: BeforeUnloadEvent) => {
    if (bypassDepth === 0 && guards.size) {
      event.preventDefault();
      event.returnValue = '';
    }
  };
  const click = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target instanceof Element ? event.target.closest('a') : null;
    if (!anchor || anchor.hasAttribute('download') || (anchor.target && anchor.target !== '_self')) return;
    const target = new URL(anchor.href, window.location.href);
    // Query/tab switches on the same page retain the mounted form.
    if (target.origin === window.location.origin && target.pathname === window.location.pathname) return;
    if (!confirmNavigation()) {
      event.preventDefault();
      event.stopImmediatePropagation();
    } else {
      runWithoutNavigationGuard(() => undefined);
    }
  };

  // Tag SPA entries without replacing Next's router state. On a rejected back
  // or forward action, restore the exact history position before Next sees it.
  const history = window.history;
  const push = history.pushState;
  const replace = history.replaceState;
  let position = Number.isSafeInteger(history.state?.[POSITION]) ? history.state[POSITION] as number : 0;
  let restoring: number | null = null;
  const tagged = (state: unknown, index: number) => ({ ...(state && typeof state === 'object' ? state : {}), [POSITION]: index });
  replace.call(history, tagged(history.state, position), '');
  const guardedPush: History['pushState'] = function (state, unused, url) {
    push.call(history, tagged(state, position + 1), unused, url);
    position += 1;
  };
  const guardedReplace: History['replaceState'] = function (state, unused, url) {
    replace.call(history, tagged(state, position), unused, url);
  };
  history.pushState = guardedPush;
  history.replaceState = guardedReplace;
  const pop = (event: PopStateEvent) => {
    const destination: unknown = event.state?.[POSITION];
    if (restoring !== null && destination === restoring) {
      restoring = null;
      event.stopImmediatePropagation();
      return;
    }
    if (typeof destination !== 'number' || !Number.isSafeInteger(destination)) return;
    if (destination !== position && !confirmNavigation()) {
      event.stopImmediatePropagation();
      restoring = position;
      history.go(position - destination);
      return;
    }
    position = destination;
  };
  window.addEventListener('beforeunload', beforeUnload);
  document.addEventListener('click', click, true);
  window.addEventListener('popstate', pop, true);
  return () => {
    window.removeEventListener('beforeunload', beforeUnload);
    document.removeEventListener('click', click, true);
    window.removeEventListener('popstate', pop, true);
    if (history.pushState === guardedPush) history.pushState = push;
    if (history.replaceState === guardedReplace) history.replaceState = replace;
  };
}

export function NavigationGuard() {
  useEffect(installNavigationGuards, []);
  return null;
}
