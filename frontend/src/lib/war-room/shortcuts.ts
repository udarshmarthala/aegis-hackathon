/**
 * Demo keyboard shortcuts.
 *
 * Every stage action has a single key so the demo never depends on mouse
 * accuracy on a projector. The one hard rule: a shortcut never fires while the
 * operator is typing - an approval note containing the letter "a" must not
 * approve a rollback.
 */

export type ShortcutAction = 'inject' | 'kill' | 'reset' | 'approve' | 'deny' | 'help';

export interface ShortcutSpec {
  key: string;
  action: ShortcutAction;
  label: string;
}

export const SHORTCUTS: readonly ShortcutSpec[] = [
  { key: 'i', action: 'inject', label: 'Inject INC-043 (bad deploy)' },
  { key: 'k', action: 'kill', label: 'Kill the worker (hard exit, no drain)' },
  { key: 'r', action: 'reset', label: 'Reset the scenario' },
  { key: 'a', action: 'approve', label: 'Approve the pending action' },
  { key: 'd', action: 'deny', label: 'Deny the pending action' },
  { key: '?', action: 'help', label: 'Show keyboard shortcuts' },
];

/** Whether the event target is somewhere the operator types. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as HTMLElement).tagName !== 'string') return false;
  const el = target as HTMLElement;
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  if (el.isContentEditable) return true;
  const editable = el.getAttribute?.('contenteditable');
  return editable !== null && editable !== undefined && editable !== 'false';
}

/** Map a key event to a demo action, or null when it must be ignored. */
export function resolveShortcut(event: {
  key: string;
  target: EventTarget | null;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  repeat?: boolean;
}): ShortcutAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return null;
  if (isTypingTarget(event.target)) return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  return SHORTCUTS.find((s) => s.key === key)?.action ?? null;
}
