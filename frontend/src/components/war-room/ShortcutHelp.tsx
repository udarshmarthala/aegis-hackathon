'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { SHORTCUTS } from '@/lib/war-room/shortcuts';
import { Kbd } from './primitives';

export const SHORTCUT_HELP = 'Keyboard shortcuts: ' + SHORTCUTS.map((s) => `${s.key.toUpperCase()} ${s.label}`).join('; ');

export function ShortcutList() {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
      {SHORTCUTS.map((s) => (
        <div key={s.key} className="contents">
          <dt><Kbd>{s.key.toUpperCase()}</Kbd></dt>
          <dd className="text-[1rem] text-ink-primary">{s.label}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ShortcutHelp({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-canvas/80" />
        <Dialog.Content className="card fixed left-1/2 top-1/2 z-50 w-[min(520px,92vw)] -translate-x-1/2 -translate-y-1/2 bg-surface-1 p-6 focus:outline-none">
          <Dialog.Title className="text-[1.3rem] font-bold">Keyboard shortcuts</Dialog.Title>
          <Dialog.Description className="mb-4 mt-1 text-[0.9rem] text-ink-secondary">
            Shortcuts are ignored while typing in a field.
          </Dialog.Description>
          <ShortcutList />
          <Dialog.Close asChild>
            <button type="button" className="mt-5 rounded-btn border border-line px-3 py-2 font-semibold hover:bg-surface-3">
              Close
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
