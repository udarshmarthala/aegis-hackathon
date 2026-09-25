'use client';

import { useEffect, useState } from 'react';
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ImageOff } from 'lucide-react';
import { cn, formatClock } from '@/lib/utils';
import { fetchIncidentMap } from '@/lib/war-room/api';
import type {
  ContextPoint, IntegrationStatus, MemoryCard, RawTreeQuery, SeqEvent, WarRoomStats,
} from '@/lib/war-room/types';
import { SourceLabel, formatTokens } from './primitives';

/**
 * The event stream (row 3) and the outcome row (row 4): the context chart that
 * carries the thesis, the memory cards with their FLUX maps, the RawTree
 * queries the agent ran against its own history, and the run's numbers.
 */

const STATUS_TONE: Record<string, string> = {
  ok: 'text-ink-tertiary',
  error: 'text-status-critical',
  rejected: 'text-status-warning',
  degraded: 'text-status-warning',
};

export function EventStream({ events, limit = 80 }: { events: readonly SeqEvent[]; limit?: number }) {
  if (events.length === 0) return <p className="text-[0.95rem] text-ink-tertiary">Waiting for events.</p>;
  return (
    <ol className="space-y-1" aria-label="Event stream, latest first">
      {events.slice(0, limit).map(({ seq, event }) => (
        <li key={seq} className="flex min-w-0 items-start gap-2 border-b border-hairline pb-1 last:border-0">
          <span className="tnum w-16 shrink-0 font-mono text-[0.72rem] text-ink-tertiary">{formatClock(event.ts)}</span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-[0.82rem] font-bold text-ink-primary">{event.event_type}</span>
              <SourceLabel source={event.source} />
              {event.tool ? <span className="font-mono text-[0.72rem] text-ink-secondary">{event.tool}</span> : null}
              <span className={cn('text-[0.7rem] font-bold uppercase', STATUS_TONE[event.status] ?? 'text-ink-secondary')}>
                {event.status}
              </span>
              <span className="ml-auto font-mono text-[0.68rem] text-ink-tertiary">s{event.step} #{seq}</span>
            </div>
            {event.message ? (
              <p className="truncate text-[0.8rem] text-ink-secondary" title={event.message}>{event.message}</p>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

export function ContextChart({ points }: { points: readonly ContextPoint[] }) {
  if (points.length === 0) {
    return <p className="text-[0.95rem] text-ink-tertiary">No steps recorded yet.</p>;
  }
  const last = points[points.length - 1];
  return (
    <figure className="flex h-full min-h-[160px] flex-col" aria-label="Context tokens per step versus a transcript-carrying agent">
      <figcaption className="sr-only">
        Latest step {last?.step}: {last?.context_tokens} context tokens against {last?.naive_tokens} naive tokens (approximate).
      </figcaption>
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={[...points]} margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
            <CartesianGrid stroke="var(--border-default)" strokeDasharray="3 3" />
            <XAxis dataKey="step" stroke="var(--text-tertiary)" fontSize={12} />
            <YAxis stroke="var(--text-tertiary)" fontSize={12} tickFormatter={(v: number) => formatTokens(v)} />
            <Tooltip
              contentStyle={{ background: 'var(--surface-2)', border: '1px solid var(--border-default)' }}
              formatter={(v: number) => formatTokens(v)}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line type="monotone" dataKey="context_tokens" name="Aegis context (approx.)" stroke="var(--accent)" strokeWidth={3} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="naive_tokens" name="Naive transcript (approx.)" stroke="var(--status-critical)" strokeWidth={2} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

function IncidentMap({ card }: { card: MemoryCard }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ready = card.image_status === 'ready';

  useEffect(() => {
    if (!ready) return undefined;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setError(null);
    fetchIncidentMap(card.id, controller.signal)
      .then((u) => {
        objectUrl = u;
        setUrl(u);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'could not load');
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [card.id, ready]);

  if (!ready || error) {
    const reason = error ?? (card.image_reason || (card.image_status === 'pending' ? 'rendering…' : 'not rendered'));
    return (
      <div className="flex h-24 w-32 shrink-0 flex-col items-center justify-center gap-1 rounded-btn border border-dashed border-line p-1 text-center">
        <ImageOff className="h-4 w-4 text-ink-tertiary" aria-hidden />
        <span className="text-[0.66rem] font-semibold leading-tight text-ink-tertiary">
          {card.image_status === 'pending' && !error ? `FLUX ${reason}` : `FLUX unavailable — ${reason}`}
        </span>
      </div>
    );
  }
  if (!url) return <div className="h-24 w-32 shrink-0 animate-pulse rounded-btn bg-surface-3" aria-label="Loading incident map" />;
  return (
    // An object URL of an authenticated fetch; next/image cannot optimise it.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={url} alt={`FLUX incident map for ${card.incident_id}`} className="h-24 w-32 shrink-0 rounded-btn border border-edge object-cover" />
  );
}

export function MemoryCards({ cards }: { cards: readonly MemoryCard[] }) {
  if (cards.length === 0) return <p className="text-[0.95rem] text-ink-tertiary">No incident memory recalled.</p>;
  return (
    <ul className="space-y-2" aria-label="Memory cards">
      {cards.map((c) => (
        <li key={c.id} className="flex gap-2 rounded-btn border border-edge bg-surface-2 p-2">
          <IncidentMap card={c} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[0.9rem] font-bold">{c.incident_id}</span>
              <SourceLabel source={c.source} />
              {c.image_status === 'ready' ? <SourceLabel source="flux" /> : null}
            </div>
            <p className="truncate text-[0.8rem] text-ink-secondary" title={c.symptoms}>{c.symptoms}</p>
            <p className="truncate text-[0.8rem] text-ink-primary" title={c.root_cause}>→ {c.root_cause}</p>
            <p className="text-[0.75rem]">
              {c.failed_actions.length ? <span className="text-status-critical">failed: {c.failed_actions.join(', ')} </span> : null}
              {c.successful_action ? <span className="text-status-success">fixed by: {c.successful_action}</span> : null}
            </p>
            {c.lesson ? <p className="truncate text-[0.75rem] italic text-ink-tertiary" title={c.lesson}>{c.lesson}</p> : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function RawTreePanel({ queries }: { queries: readonly RawTreeQuery[] }) {
  if (queries.length === 0) return <p className="text-[0.95rem] text-ink-tertiary">No queries run yet.</p>;
  return (
    <ul className="space-y-2" aria-label="RawTree queries">
      {queries.map((q, i) => (
        <li key={`${q.ts}-${q.name}-${i}`} className="rounded-btn border border-edge bg-surface-2 p-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-[0.82rem] font-bold">{q.name}</span>
            <SourceLabel source={q.source} />
            <span className="tnum ml-auto text-[0.72rem] text-ink-secondary">
              {q.error ? <span className="font-bold text-status-critical">error</span> : `${q.rows} rows`} · {q.duration_ms} ms
            </span>
          </div>
          {q.sql ? (
            <pre className="mt-1 max-h-16 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.7rem] leading-snug text-ink-secondary">{q.sql}</pre>
          ) : null}
          {q.error ? <p className="mt-0.5 text-[0.72rem] text-status-critical">{q.error}</p> : null}
        </li>
      ))}
    </ul>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-btn border border-edge bg-surface-2 px-2 py-1.5" title={hint}>
      <dt className="text-[0.68rem] font-semibold uppercase tracking-wider text-ink-tertiary">{label}</dt>
      <dd className="tnum text-[1.3rem] font-bold leading-tight">{value}</dd>
    </div>
  );
}

export function StatsPanel({
  stats,
  integrations,
}: {
  stats: WarRoomStats | null;
  integrations: Record<string, IntegrationStatus>;
}) {
  const ratio = stats && Number.isFinite(stats.compression_ratio) && stats.compression_ratio > 0
    ? `${stats.compression_ratio.toFixed(1)}×`
    : '—';
  const names = Object.keys(integrations);
  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-2 gap-2">
        <Stat label="Compression" value={ratio} hint="Raw observation tokens per card token" />
        <Stat label="Cache hits" value={stats ? String(stats.cache_hits) : '—'} hint={stats ? `${formatTokens(stats.cache_read_tokens)} cached tokens read` : undefined} />
        <Stat label="Fallbacks used" value={stats ? String(stats.fallbacks_used) : '—'} />
        <Stat label="Steps" value={stats ? String(stats.steps) : '—'} />
        <Stat label="Context" value={stats ? formatTokens(stats.context_tokens) : '—'} hint="approx." />
        <Stat label="Naive" value={stats ? formatTokens(stats.naive_tokens) : '—'} hint="approx." />
      </dl>
      {names.length ? (
        <ul className="flex flex-wrap gap-1" aria-label="Integrations">
          {names.map((name) => {
            const s = integrations[name];
            return (
              <li
                key={name}
                title={s?.reason || undefined}
                className={cn(
                  'rounded border px-1.5 py-px text-[0.7rem] font-semibold',
                  s?.configured ? 'border-status-success/40 text-status-success' : 'border-status-warning/50 text-status-warning',
                )}
              >
                {name}: {s?.configured ? 'on' : 'fallback'}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
