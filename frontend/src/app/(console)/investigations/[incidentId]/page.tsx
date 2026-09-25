'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, ShieldQuestion } from 'lucide-react';
import { api } from '@/lib/api';
import { consoleApi } from '@/lib/console-api';
import { SeverityBadge, StateChip } from '@/components/ui/primitives';
import { SkeletonRows } from '@/components/ui/states';
import { AgentRunTimeline } from '@/components/investigations/AgentRunTimeline';
import { EvidenceGapPanel } from '@/components/investigations/EvidenceGapPanel';
import { ToolCallTable } from '@/components/investigations/ToolCallTable';
import { QueryFailure, SectionCard } from '@/components/reliability/panels';
import { formatDuration } from '@/lib/utils';

/**
 * The transparency surface for one investigation.
 *
 * Three independent queries, three independent failure states. If tool calls
 * cannot be read, the agent timeline still renders - collapsing them into one
 * "investigation unavailable" would hide work that was recorded perfectly well.
 */
export default function InvestigationDetailPage() {
  const params = useParams<{ incidentId: string }>();
  const incidentId = params.incidentId;

  const incident = useQuery({
    queryKey: ['incident', incidentId],
    queryFn: () => api.getIncident(incidentId),
    retry: false,
  });

  const runs = useQuery({
    queryKey: ['investigation', 'runs', incidentId],
    queryFn: () => consoleApi.agentRuns(incidentId),
    refetchInterval: 20_000,
  });

  const tools = useQuery({
    queryKey: ['investigation', 'tools', incidentId],
    queryFn: () => consoleApi.toolCalls(incidentId),
    refetchInterval: 20_000,
  });

  const gaps = useQuery({
    queryKey: ['investigation', 'gaps', incidentId],
    queryFn: () => consoleApi.evidenceGaps(incidentId),
    refetchInterval: 30_000,
  });

  const runRows = runs.data?.items ?? [];
  const failedRuns = runRows.filter((run) => run.status.toUpperCase().includes('FAIL')).length;
  const agentTime = runRows.reduce((total, run) => total + (run.duration_ms ?? 0), 0);

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-7">
      <Link
        href="/investigations"
        className="mb-4 inline-flex items-center gap-1.5 text-meta font-semibold text-ink-tertiary
                   transition-colors duration-hover hover:text-ink-primary"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        All investigations
      </Link>

      <header className="mb-5">
        <div className="flex flex-wrap items-center gap-3">
          {incident.data ? <SeverityBadge severity={incident.data.severity} /> : null}
          <h1 className="text-h2 font-semibold tracking-tight">
            {incident.data?.title ?? 'Investigation'}
          </h1>
          {incident.data ? <StateChip state={incident.data.state} /> : null}
        </div>
        <p className="mt-1 font-mono text-meta font-medium text-ink-tertiary">{incidentId}</p>
        {incident.isError ? (
          <p className="mt-1 text-meta font-semibold text-status-warning">
            Incident header unavailable: {(incident.error as Error).message}. The investigation
            record below is read separately and is unaffected.
          </p>
        ) : null}
      </header>

      <div className="mb-4 card flex items-start gap-3 p-4">
        <ShieldQuestion className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden />
        <p className="text-meta font-medium text-ink-secondary">
          <span className="font-semibold text-ink-primary">
            This is a record of work, not of reasoning.
          </span>{' '}
          Aegis stores the task each agent was given, the tools it called, the arguments it sent,
          what came back and what it concluded. Model chain-of-thought is never captured and never
          displayed.
        </p>
      </div>

      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Agent runs</p>
          <p className="tnum mt-1 text-h2 font-semibold text-ink-primary">
            {runs.isLoading ? '—' : runRows.length}
          </p>
        </div>
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Failed runs</p>
          <p
            className={
              failedRuns > 0
                ? 'tnum mt-1 text-h2 font-semibold text-status-critical'
                : 'tnum mt-1 text-h2 font-semibold text-ink-primary'
            }
          >
            {runs.isLoading ? '—' : failedRuns}
          </p>
        </div>
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Agent time</p>
          <p className="tnum mt-1 text-h2 font-semibold text-ink-primary">
            {runs.isLoading ? '—' : formatDuration(agentTime)}
          </p>
        </div>
      </div>

      <div className="space-y-4">
        <SectionCard
          title="Evidence gaps"
          description="Sources Aegis meant to consult and could not, shown next to the evidence it could use."
        >
          {gaps.isLoading ? (
            <SkeletonRows rows={2} />
          ) : gaps.isError ? (
            <QueryFailure
              error={gaps.error}
              title="Cannot load evidence gaps"
              source="Evidence store"
              consequence="Whether anything was missing during this investigation is unknown — which is not the same as nothing being missing."
              onRetry={() => gaps.refetch()}
            />
          ) : gaps.data ? (
            <EvidenceGapPanel
              gaps={gaps.data.items}
              usableEvidenceCount={gaps.data.usable_evidence_count}
            />
          ) : null}
        </SectionCard>

        <SectionCard
          title="Agent runs"
          description="Each capability agent in dispatch order, with its task, model, prompt version and result."
        >
          {runs.isLoading ? (
            <SkeletonRows rows={4} />
          ) : runs.isError ? (
            <QueryFailure
              error={runs.error}
              title="Cannot load agent runs"
              source="Agent run record"
              consequence="The investigation timeline is unavailable. Tool calls and evidence gaps are read separately."
              onRetry={() => runs.refetch()}
            />
          ) : (
            <AgentRunTimeline runs={runRows} />
          )}
        </SectionCard>

        <SectionCard
          title="Tool calls"
          description="Every retrieval made during this investigation, with the exact arguments sent."
        >
          {tools.isLoading ? (
            <SkeletonRows rows={4} />
          ) : tools.isError ? (
            <QueryFailure
              error={tools.error}
              title="Cannot load tool calls"
              source="Tool call record"
              consequence="What Aegis queried cannot be replayed from here. The agent timeline above is unaffected."
              onRetry={() => tools.refetch()}
            />
          ) : (
            <ToolCallTable calls={tools.data?.items ?? []} />
          )}
        </SectionCard>
      </div>
    </div>
  );
}
