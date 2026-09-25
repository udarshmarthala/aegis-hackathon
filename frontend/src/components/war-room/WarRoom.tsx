'use client';

import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import Link from 'next/link';
import { toast } from 'sonner';
import { ArrowLeft, Keyboard, Power, RotateCcw, Syringe, type LucideIcon } from 'lucide-react';
import { ApiError, NetworkError } from '@/lib/api';
import { cn } from '@/lib/utils';
import { STREAM_PATH, warRoomApi } from '@/lib/war-room/api';
import {
  approvalOpen, initialModel, isSeqEvent, warRoomReducer, type ConnectionState, type WarRoomModel,
} from '@/lib/war-room/reducer';
import { openStream } from '@/lib/war-room/sse';
import type { ControlResponse, HealthPayload, WarRoomState } from '@/lib/war-room/types';
import { AegisMark } from '@/components/shell/AegisMark';
import { ApprovalModal } from './ApprovalModal';
import { HealthCards } from './HealthCards';
import { ContextChart, EventStream, MemoryCards, RawTreePanel, StatsPanel } from './Outcome';
import { PhaseChain } from './PhaseChain';
import { Kbd, Panel, SourceLabel } from './primitives';
import { EvidenceCards, GoalTree, HypothesisBars } from './Reasoning';
import { SHORTCUT_HELP, ShortcutHelp } from './ShortcutHelp';
import { useDemoShortcuts } from './useDemoShortcuts';

/**
 * The war room: one projector-sized page that shows a long-horizon incident
 * run end to end.
 *
 * Data flow is read-once-then-stream. Four reads describe the present; after
 * that the page only listens to `/v1/war-room/stream` and never polls, so what
 * is on screen is exactly what the backend published, in order, deduplicated
 * by sequence number.
 */

function failureText(err: unknown): string {
  if (err instanceof ApiError) return `${err.message || err.code} (${err.status})`;
  if (err instanceof NetworkError) return err.message;
  return 'The request did not complete.';
}

const CONNECTION_TEXT: Record<ConnectionState, { text: string; tone: string }> = {
  connecting: { text: 'Connecting', tone: 'text-ink-secondary' },
  live: { text: 'Live', tone: 'text-status-success' },
  reconnecting: { text: 'Reconnecting', tone: 'text-status-warning' },
  offline: { text: 'Offline', tone: 'text-status-critical' },
};

type ControlKey = 'inject' | 'kill' | 'reset';

function ControlButton({
  icon: Icon,
  label,
  shortcut,
  busy,
  onClick,
  tone = 'default',
}: {
  icon: LucideIcon;
  label: string;
  shortcut: string;
  busy: boolean;
  onClick: () => void;
  tone?: 'default' | 'danger';
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      aria-keyshortcuts={shortcut}
      aria-label={`${label} (shortcut ${shortcut})`}
      className={cn(
        'inline-flex items-center gap-2 rounded-btn border px-3 py-2 text-[0.95rem] font-bold',
        'transition-colors duration-hover disabled:opacity-50',
        tone === 'danger'
          ? 'border-status-critical/50 text-status-critical hover:bg-status-critical/10'
          : 'border-edge text-ink-primary hover:bg-surface-3',
      )}
    >
      <Icon className="h-4 w-4" aria-hidden />
      {busy ? `${label}…` : label}
      <Kbd>{shortcut}</Kbd>
    </button>
  );
}

function TopBar({
  model,
  busy,
  onControl,
  onHelp,
}: {
  model: WarRoomModel;
  busy: Record<ControlKey, boolean>;
  onControl: (key: ControlKey) => void;
  onHelp: () => void;
}) {
  const conn = CONNECTION_TEXT[model.connection];
  const badge = model.brainBadge;
  return (
    <header className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-card border border-hairline bg-surface-1 px-4 py-2">
      <Link href="/overview" className="inline-flex items-center gap-2 text-ink-secondary hover:text-ink-primary" aria-label="Back to the console">
        <ArrowLeft className="h-4 w-4" aria-hidden />
        <AegisMark className="h-5 w-5" />
      </Link>
      <h1 className="text-[1.35rem] font-bold tracking-tight">War room</h1>
      <div className="flex min-w-0 flex-col leading-tight">
        <span className="text-[0.68rem] font-semibold uppercase tracking-wider text-ink-tertiary">run</span>
        <span className="truncate font-mono text-[0.9rem] font-bold" data-testid="run-id">
          {model.run?.run_id ?? 'no run'}
        </span>
      </div>
      {model.incident ? (
        <div className="flex min-w-0 max-w-[26rem] flex-col leading-tight">
          <span className="text-[0.68rem] font-semibold uppercase tracking-wider text-ink-tertiary">
            {model.incident.id} · {model.incident.severity}
          </span>
          <span className="truncate text-[0.9rem] font-semibold" title={model.incident.title}>{model.incident.title}</span>
        </div>
      ) : null}
      <span
        data-testid="mode-badge"
        className={cn(
          'rounded border px-2 py-0.5 text-[0.85rem] font-bold uppercase',
          model.mode === 'live' ? 'border-status-success/50 text-status-success' : 'border-status-warning/60 text-status-warning',
        )}
      >
        mode: {model.mode ?? 'unknown'}
      </span>
      <span className="inline-flex items-center gap-1.5" data-testid="brain-badge">
        <span className="text-[0.75rem] font-semibold uppercase tracking-wider text-ink-tertiary">brain</span>
        {badge ? <SourceLabel source={badge.source} size="md" /> : <span className="text-ink-tertiary">—</span>}
        {badge?.model ? <span className="font-mono text-[0.8rem] text-ink-secondary">{badge.model}</span> : null}
      </span>
      <span className={cn('inline-flex items-center gap-1.5 text-[0.85rem] font-bold', conn.tone)} role="status" aria-live="polite">
        <span aria-hidden className={cn('h-2 w-2 rounded-full bg-current', model.connection === 'live' && 'motion-safe:animate-pulse-soft')} />
        {conn.text}
      </span>
      <div className="ml-auto flex flex-wrap items-center gap-2" role="toolbar" aria-label="Demo controls">
        <ControlButton icon={Syringe} label="Inject INC-043" shortcut="I" busy={busy.inject} onClick={() => onControl('inject')} />
        <ControlButton icon={Power} label="Kill worker" shortcut="K" busy={busy.kill} onClick={() => onControl('kill')} tone="danger" />
        <ControlButton icon={RotateCcw} label="Reset" shortcut="R" busy={busy.reset} onClick={() => onControl('reset')} />
        <button
          type="button"
          onClick={onHelp}
          aria-keyshortcuts="?"
          aria-label="Keyboard shortcuts (shortcut ?)"
          className="inline-flex items-center gap-1.5 rounded-btn border border-edge px-2.5 py-2 font-bold hover:bg-surface-3"
        >
          <Keyboard className="h-4 w-4" aria-hidden />
          <Kbd>?</Kbd>
        </button>
      </div>
      <p className="sr-only">{SHORTCUT_HELP}</p>
    </header>
  );
}

export function WarRoom() {
  const [model, dispatch] = useReducer(warRoomReducer, initialModel);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<ControlKey, boolean>>({ inject: false, kill: false, reset: false });
  const [deciding, setDeciding] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const modelRef = useRef(model);
  modelRef.current = model;

  // Read once, then stream. The stream opens after the reads so its
  // Last-Event-ID resumes exactly where the history ended.
  useEffect(() => {
    let cancelled = false;
    let close: (() => void) | null = null;

    (async () => {
      const [state, events, series, health, queries] = await Promise.allSettled([
        warRoomApi.state(),
        warRoomApi.events(null, 0, 500),
        warRoomApi.contextSeries(null),
        warRoomApi.health(),
        warRoomApi.rawtreeQueries(null),
      ]);
      if (cancelled) return;
      let resumeFrom = 0;
      if (state.status === 'fulfilled') {
        const history = events.status === 'fulfilled' ? events.value.events ?? [] : [];
        resumeFrom = Math.max(state.value.last_seq ?? 0, ...history.map((e) => e.seq ?? 0));
        setLoadError(null);
        dispatch({
          type: 'init',
          state: state.value,
          events: events.status === 'fulfilled' ? events.value.events ?? [] : [],
          points: series.status === 'fulfilled' ? series.value.points ?? [] : [],
          health: health.status === 'fulfilled' ? health.value : null,
          queries: queries.status === 'fulfilled' ? queries.value.queries ?? [] : [],
        });
      } else {
        // The stream opens with a snapshot, so the page can still come up.
        setLoadError(failureText(state.reason));
      }

      close = openStream({
        path: STREAM_PATH,
        lastEventId: resumeFrom > 0 ? String(resumeFrom) : null,
        onStatus: (status) => dispatch({ type: 'connection', state: status }),
        onFrame: (frame) => {
          if (frame.event === 'ping') return;
          let data: unknown;
          try {
            data = JSON.parse(frame.data);
          } catch {
            console.warn('war room: dropped a malformed stream frame', frame.event);
            return;
          }
          if (frame.event === 'snapshot') {
            setLoadError(null);
            dispatch({ type: 'snapshot', state: data as WarRoomState });
          } else if (frame.event === 'horizon' && isSeqEvent(data)) {
            dispatch({ type: 'horizon', frame: data });
          } else if (frame.event === 'health') {
            dispatch({ type: 'health', health: data as HealthPayload });
          }
        },
      });
    })();

    return () => {
      cancelled = true;
      close?.();
    };
  }, []);

  const runControl = useCallback(async (key: ControlKey) => {
    setBusy((b) => (b[key] ? b : { ...b, [key]: true }));
    const call: () => Promise<ControlResponse> =
      key === 'inject' ? () => warRoomApi.inject('INC-043') : key === 'kill' ? warRoomApi.killWorker : warRoomApi.reset;
    const label = key === 'inject' ? 'Inject INC-043' : key === 'kill' ? 'Kill worker' : 'Reset';
    try {
      const res = await call();
      if (res.ok) toast.success(`${label}: ${res.detail}`);
      else toast.error(`${label}: ${res.detail}`);
    } catch (err) {
      toast.error(`${label} failed: ${failureText(err)}`);
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }, []);

  const decide = useCallback(async (approve: boolean) => {
    const pending = modelRef.current.approval;
    if (!pending) {
      toast.message('No approval is pending.');
      return;
    }
    setDeciding(true);
    try {
      await (approve ? warRoomApi.approve(pending.approval_id) : warRoomApi.deny(pending.approval_id));
      dispatch({ type: 'approval-settled', approvalId: pending.approval_id });
      toast.success(`${approve ? 'Approved' : 'Denied'} ${pending.action_type} on ${pending.target}`);
    } catch (err) {
      toast.error(`Decision not recorded: ${failureText(err)}`);
    } finally {
      setDeciding(false);
    }
  }, []);

  // A dismissed request is brought back first rather than decided unseen: the
  // key press that decides must follow one that showed what is being decided.
  const decideByKey = (approve: boolean) => {
    if (model.approval && model.approvalDismissed) dispatch({ type: 'approval-reopened' });
    else void decide(approve);
  };

  useDemoShortcuts({
    inject: () => void runControl('inject'),
    kill: () => void runControl('kill'),
    reset: () => void runControl('reset'),
    approve: () => decideByKey(true),
    deny: () => decideByKey(false),
    help: () => setHelpOpen((o) => !o),
  });

  const run = model.run;

  return (
    <div className="flex min-h-screen flex-col gap-2.5 bg-canvas p-3 text-[15px] xl:h-screen xl:overflow-hidden">
      <TopBar model={model} busy={busy} onControl={(k) => void runControl(k)} onHelp={() => setHelpOpen(true)} />

      {loadError ? (
        <p role="alert" className="rounded-btn border border-status-critical/50 bg-status-critical/10 px-3 py-1.5 font-semibold text-status-critical">
          War-room state could not be read: {loadError}. Waiting for the stream snapshot.
        </p>
      ) : null}

      <HealthCards health={model.health} />

      <div className="grid gap-2.5 xl:h-[200px] xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <Panel
          title="Phase"
          right={
            run ? (
              <span className="tnum font-mono text-[0.8rem] text-ink-secondary">
                step {run.step} · cycle {run.remediation_cycle}
              </span>
            ) : null
          }
        >
          <PhaseChain phase={model.phase} visited={model.visited} />
          {run?.escalation_reason ? (
            <p className="mt-1 text-[0.85rem] font-bold text-status-critical">Escalated: {run.escalation_reason}</p>
          ) : null}
        </Panel>
        <Panel title="Hypotheses" right={<span className="text-[0.7rem] text-ink-tertiary">confidence derived from cited evidence</span>}>
          <HypothesisBars hypotheses={run?.hypotheses ?? []} />
        </Panel>
      </div>

      <div className="grid min-h-[360px] gap-2.5 xl:min-h-0 xl:flex-1 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.7fr)_minmax(0,1.25fr)]">
        <Panel title="Goals">
          <GoalTree goals={run?.goals ?? []} />
        </Panel>
        <Panel
          title="Evidence in context"
          right={
            <span className="tnum text-[0.75rem] text-ink-secondary">
              {model.evidence.filter((v) => !v.discarded).length} / 12 cards
            </span>
          }
        >
          <EvidenceCards evidence={model.evidence} />
        </Panel>
        <Panel title="Event stream" right={<span className="tnum text-[0.75rem] text-ink-secondary">last #{model.lastSeq}</span>}>
          <EventStream events={model.events} />
        </Panel>
      </div>

      <div className="grid gap-2.5 xl:h-[250px] xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_minmax(0,0.95fr)]">
        <Panel title="Context per step" right={<span className="text-[0.72rem] font-semibold text-ink-tertiary">tokens, approx.</span>} bodyClassName="p-2">
          <ContextChart points={model.contextPoints} />
        </Panel>
        <Panel title="Incident memory">
          <MemoryCards cards={model.memoryCards} />
        </Panel>
        <Panel title="RawTree queries">
          <RawTreePanel queries={model.rawtreeQueries} />
        </Panel>
        <Panel title="Stats">
          <StatsPanel stats={model.stats} integrations={model.integrations} />
        </Panel>
      </div>

      <ApprovalModal
        approval={model.approval}
        open={approvalOpen(model)}
        pending={deciding}
        onApprove={() => void decide(true)}
        onDeny={() => void decide(false)}
        onDismiss={() => dispatch({ type: 'approval-dismissed' })}
      />
      <ShortcutHelp open={helpOpen} onOpenChange={setHelpOpen} />
    </div>
  );
}
