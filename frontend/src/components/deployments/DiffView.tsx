'use client';

import { useMemo } from 'react';
import { cn } from '@/lib/utils';

/**
 * A unified diff, rendered as a diff.
 *
 * Patch review should read like a premium git client rather than a chat
 * transcript (UX spec 35), so added and removed lines carry the status tokens
 * and both old and new line numbers are shown - reviewing a repair without
 * knowing where in the file it lands is not review.
 */

type LineKind = 'add' | 'del' | 'hunk' | 'meta' | 'context';

interface DiffLine {
  kind: LineKind;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

function parseDiff(diff: string): DiffLine[] {
  const lines: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;

  for (const raw of diff.replace(/\r\n/g, '\n').split('\n')) {
    const hunk = HUNK.exec(raw);
    if (hunk) {
      oldNo = Number(hunk[1] ?? 1);
      newNo = Number(hunk[2] ?? 1);
      lines.push({ kind: 'hunk', text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (
      raw.startsWith('diff ') || raw.startsWith('index ') || raw.startsWith('--- ') ||
      raw.startsWith('+++ ') || raw.startsWith('new file') || raw.startsWith('deleted file') ||
      raw.startsWith('similarity index') || raw.startsWith('rename ') || raw.startsWith('\\')
    ) {
      lines.push({ kind: 'meta', text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (raw.startsWith('+')) {
      lines.push({ kind: 'add', text: raw, oldNo: null, newNo: newNo++ });
      continue;
    }
    if (raw.startsWith('-')) {
      lines.push({ kind: 'del', text: raw, oldNo: oldNo++, newNo: null });
      continue;
    }
    lines.push({ kind: 'context', text: raw, oldNo: oldNo++, newNo: newNo++ });
  }
  return lines;
}

const ROW_TONE: Record<LineKind, string> = {
  add: 'bg-status-success/10 text-status-success',
  del: 'bg-status-critical/10 text-status-critical',
  hunk: 'bg-surface-3 text-accent',
  meta: 'text-ink-tertiary',
  context: 'text-ink-secondary',
};

export function DiffView({ diff, label }: { diff: string; label: string }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  const added = lines.filter((line) => line.kind === 'add').length;
  const removed = lines.filter((line) => line.kind === 'del').length;

  return (
    <div className="min-w-0">
      <p className="label-meta mb-1.5 font-semibold">
        {label} ·{' '}
        <span className="tnum font-bold text-status-success">+{added}</span>{' '}
        <span className="tnum font-bold text-status-critical">-{removed}</span>
      </p>
      <div className="max-h-[420px] overflow-auto rounded-btn border border-hairline bg-surface-2">
        <table className="w-full border-collapse font-mono text-meta font-medium leading-relaxed">
          <caption className="sr-only">{label}, unified diff</caption>
          <thead className="sr-only">
            <tr>
              <th scope="col">Line before</th>
              <th scope="col">Line after</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, index) => (
              <tr key={`${index}-${line.text.slice(0, 12)}`} className={cn(ROW_TONE[line.kind])}>
                <td className="tnum w-12 select-none border-r border-hairline px-2 py-0.5 text-right align-top text-ink-tertiary">
                  {line.oldNo ?? ''}
                </td>
                <td className="tnum w-12 select-none border-r border-hairline px-2 py-0.5 text-right align-top text-ink-tertiary">
                  {line.newNo ?? ''}
                </td>
                <td className="whitespace-pre-wrap break-all px-2 py-0.5 align-top">
                  {line.text || ' '}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
