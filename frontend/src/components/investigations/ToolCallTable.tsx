'use client';

import { EmptyState } from '@/components/ui/states';
import { cn, formatClock, formatDuration, relativeTime } from '@/lib/utils';
import type { ToolCallRow } from '@/lib/console-types';
import { STATUS_TONE_CLASS, runStatusTone } from './domain';

/**
 * Every tool call an investigation made.
 *
 * Arguments are shown verbatim because reproducing a query is how an engineer
 * checks an agent's work. They are rendered as text and never interpreted:
 * tool payloads carry untrusted operational strings, which are data.
 */
export function ToolCallTable({ calls }: { calls: ToolCallRow[] }) {
  if (calls.length === 0) {
    return (
      <EmptyState
        title="No tool call is recorded for this incident."
        detail="Aegis records every retrieval it makes. None here means none were made, not that they were not captured."
      />
    );
  }

  return (
    <table className="w-full border-collapse text-body">
      <caption className="sr-only">Tool calls made during this investigation, newest first</caption>
      <thead>
        <tr className="border-b border-hairline text-left">
          {['Tool', 'Status', 'Duration', 'When', 'Arguments'].map((header) => (
            <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
              {header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {calls.map((call) => {
          const tone = runStatusTone(call.status);
          return (
            <tr key={call.id} className="border-b border-hairline align-top last:border-0">
              <th scope="row" className="max-w-[220px] px-3 py-2 text-left">
                <span className="block truncate font-mono font-semibold text-ink-primary">
                  {call.tool_name}
                </span>
                {call.error ? (
                  <span className="mt-0.5 block break-words text-meta font-semibold text-status-critical">
                    {call.error}
                  </span>
                ) : null}
              </th>
              <td className="px-3 py-2">
                <span
                  className={cn(
                    'rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                    STATUS_TONE_CLASS[tone],
                  )}
                >
                  {call.status}
                </span>
              </td>
              <td className="tnum px-3 py-2 font-semibold text-ink-secondary">
                {typeof call.duration_ms === 'number' ? formatDuration(call.duration_ms) : '—'}
              </td>
              <td className="px-3 py-2 font-medium text-ink-tertiary">
                <span className="tnum">{formatClock(call.created_at)}</span>
                <span className="block text-meta">{relativeTime(call.created_at)}</span>
              </td>
              <td className="max-w-[420px] px-3 py-2">
                {call.arguments === null || Object.keys(call.arguments).length === 0 ? (
                  <span className="text-meta font-medium text-ink-tertiary">No arguments</span>
                ) : (
                  <details className="group">
                    <summary className="cursor-pointer text-meta font-semibold text-ink-secondary">
                      {Object.keys(call.arguments).length} argument
                      {Object.keys(call.arguments).length === 1 ? '' : 's'}
                    </summary>
                    <pre className="mt-1.5 max-h-52 overflow-auto rounded border border-hairline
                                    bg-surface-2 p-2 font-mono text-meta font-medium text-ink-secondary">
                      {JSON.stringify(call.arguments, null, 2)}
                    </pre>
                  </details>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
