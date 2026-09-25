'use client';

import { useEffect, useRef } from 'react';
import { resolveShortcut, type ShortcutAction } from '@/lib/war-room/shortcuts';

export type ShortcutHandlers = Partial<Record<ShortcutAction, () => void>>;

/**
 * Bind the demo shortcuts to the document. Handlers are read through a ref so
 * the listener is attached once and always calls the latest closure - a stale
 * handler would approve the approval that was pending when the page mounted.
 */
export function useDemoShortcuts(handlers: ShortcutHandlers, enabled = true): void {
  const ref = useRef(handlers);
  ref.current = handlers;

  useEffect(() => {
    if (!enabled) return undefined;
    function onKey(event: KeyboardEvent) {
      const action = resolveShortcut(event);
      if (!action) return;
      const handler = ref.current[action];
      if (!handler) return;
      event.preventDefault();
      handler();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [enabled]);
}
