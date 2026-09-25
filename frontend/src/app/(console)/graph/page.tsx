'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { List, Network, RefreshCw } from 'lucide-react';
import { NetworkError } from '@/lib/api';
import { consoleApi } from '@/lib/console-api';
import { isAvailable } from '@/lib/console-types';
import {
  EmptyState,
  ErrorState,
  SkeletonRows,
  SourceUnavailableState,
  WorkingIndicator,
} from '@/components/ui/states';
import { ForceGraph } from '@/components/graph/ForceGraph';
import { NodeDetailPanel } from '@/components/graph/NodeDetailPanel';
import { TopologyList } from '@/components/graph/TopologyList';
import { HealthLegend } from '@/components/graph/HealthLegend';
import { computeLayout } from '@/components/graph/graph-model';
import { cn } from '@/lib/utils';

/**
 * The service graph.
 *
 * Topology is a claim about the environment, so every failure mode is stated
 * rather than drawn: an unreachable graph store renders the reason and falls
 * back to the service inventory from the system of record, because an empty
 * canvas would tell an operator their architecture has no dependencies.
 */

const DEPTHS = [1, 2, 3, 4] as const;
type Depth = (typeof DEPTHS)[number];

function readNumberField(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function readStringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function describeRefresh(result: Record<string, unknown>): string {
  if (result.available === false) {
    return `Topology refresh refused: ${readStringField(result, 'reason') ?? 'no reason given'}.`;
  }
  const services = readNumberField(result, 'services');
  const edges = readNumberField(result, 'edges');
  const parts: string[] = [];
  parts.push(services === null ? 'services not re-ingested' : `${services} services re-ingested`);
  parts.push(edges === null ? 'edges not re-ingested' : `${edges} edges re-ingested`);
  const serviceError = readStringField(result, 'services_error');
  const edgeError = readStringField(result, 'edges_error');
  if (serviceError) parts.push(`service ingest: ${serviceError}`);
  if (edgeError) parts.push(`edge ingest: ${edgeError}`);
  return `${parts.join(' · ')}.`;
}

function GraphConsole() {
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const requestedService = searchParams.get('service');
  const [serviceId, setServiceId] = useState<string>('');
  const [depth, setDepth] = useState<Depth>(2);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mode, setMode] = useState<'graph' | 'list'>('graph');
  const [announcement, setAnnouncement] = useState('');

  const services = useQuery({
    queryKey: ['systems', 'services'],
    queryFn: () => consoleApi.services(),
    staleTime: 60_000,
  });

  // Topology outlives the runtime adapter's reach: when no runtime inventory
  // is available, pick from the services the graph itself knows.
  const runtimeUnavailable = services.data !== undefined && !isAvailable(services.data);
  const graphServices = useQuery({
    queryKey: ['graph', 'services'],
    queryFn: () => consoleApi.graphServices(),
    enabled: runtimeUnavailable,
    staleTime: 60_000,
  });

  const serviceRows = useMemo((): Array<{ service_id: string; name: string; health?: string }> => {
    if (services.data && isAvailable(services.data)) return services.data.items;
    if (graphServices.data && isAvailable(graphServices.data)) return graphServices.data.items;
    return [];
  }, [services.data, graphServices.data]);

  useEffect(() => {
    if (serviceId !== '') return;
    // `?service=` is how the systems page hands a service over ("Open in
    // service graph"). It is honoured only once the inventory confirms the id,
    // because a stale or mistyped link must land on a real service rather than
    // on a picker showing a selection the list does not contain.
    const deepLinked =
      requestedService !== null && serviceRows.some((row) => row.service_id === requestedService)
        ? requestedService
        : null;
    const fallback = serviceRows[0]?.service_id ?? null;
    const next = deepLinked ?? fallback;
    if (next) setServiceId(next);
  }, [requestedService, serviceId, serviceRows]);

  const neighbourhood = useQuery({
    queryKey: ['graph', 'neighbourhood', serviceId, depth],
    queryFn: () => consoleApi.neighbourhood(serviceId, depth),
    enabled: serviceId !== '',
    staleTime: 30_000,
  });

  const layout = useMemo(() => {
    const data = neighbourhood.data;
    if (!data || !isAvailable(data)) return null;
    return computeLayout(data.nodes, data.edges, data.root);
  }, [neighbourhood.data]);

  // Selection drives the blast-radius highlight. The detail panel asks for the
  // same key, so this is one request shared by both, not two.
  const radius = useQuery({
    queryKey: ['graph', 'blast-radius', selectedId ?? ''],
    queryFn: () => consoleApi.blastRadius(selectedId ?? ''),
    enabled: selectedId !== null,
    staleTime: 30_000,
  });

  const blastRadius = useMemo(() => {
    const data = radius.data;
    if (!data || !isAvailable(data)) return new Set<string>();
    return new Set<string>([...data.directly_affected, ...data.downstream]);
  }, [radius.data]);

  // The canvas can only be handed a set of ids, so a failed query and a genuinely
  // empty blast radius arrive there identically: no highlighted ring. Unringed
  // nodes read as "nothing downstream is affected", which is the most dangerous
  // sentence this page could accidentally say, so the failure is stated above the
  // canvas and the legend stops promising a highlight that is not being drawn.
  const radiusUnknown =
    selectedId !== null &&
    (radius.isError || (radius.data !== undefined && !isAvailable(radius.data)));
  const radiusPending = selectedId !== null && radius.isLoading;

  const selectedNode = selectedId ? (layout?.byId.get(selectedId) ?? null) : null;

  useEffect(() => {
    // A new neighbourhood may not contain the previous selection.
    if (selectedId && layout && !layout.byId.has(selectedId)) setSelectedId(null);
  }, [layout, selectedId]);

  const refresh = useMutation({
    mutationFn: () => consoleApi.refreshTopology(),
    onSuccess: (result) => {
      const message = describeRefresh(result);
      setAnnouncement(message);
      if (result.available === false) toast.warning(message);
      else toast.success(message);
      queryClient.invalidateQueries({ queryKey: ['graph'] });
    },
    onError: (error) => {
      const message = `Topology refresh failed: ${(error as Error).message}`;
      setAnnouncement(message);
      toast.error(message);
    },
  });

  function select(id: string | null) {
    setSelectedId(id);
    const node = id && layout ? layout.byId.get(id) : null;
    setAnnouncement(node ? `${node.name} selected. ${node.degree} connections.` : 'Selection cleared.');
  }

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-h2 font-semibold tracking-tight">Service graph</h1>
          <p className="mt-1 text-body font-medium text-ink-secondary">
            Call and dependency topology around one service, with the blast radius of whatever you
            select.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-meta font-semibold text-ink-tertiary">
            <span className="uppercase tracking-wider">Service</span>
            <select
              value={serviceId}
              onChange={(event) => {
                setServiceId(event.target.value);
                setSelectedId(null);
              }}
              className="rounded-btn border border-line bg-surface-2 px-2 py-1.5 text-body
                         font-medium text-ink-primary"
            >
              {serviceRows.length === 0 ? <option value="">No service inventory</option> : null}
              {serviceRows.map((row) => (
                <option key={row.service_id} value={row.service_id}>
                  {row.name}
                </option>
              ))}
            </select>
          </label>

          <div className="flex items-center gap-1" role="group" aria-label="Traversal depth">
            <span className="mr-1 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
              Depth
            </span>
            {DEPTHS.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setDepth(value)}
                aria-pressed={depth === value}
                aria-label={`Depth ${value}`}
                className={cn(
                  'tnum rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
                  depth === value
                    ? 'border-edge bg-surface-3 text-ink-primary'
                    : 'border-hairline text-ink-tertiary hover:bg-surface-2',
                )}
              >
                {value}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-1" role="group" aria-label="View mode">
            <button
              type="button"
              onClick={() => setMode('graph')}
              aria-pressed={mode === 'graph'}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn border px-2 py-1 text-meta font-semibold',
                mode === 'graph'
                  ? 'border-edge bg-surface-3 text-ink-primary'
                  : 'border-hairline text-ink-tertiary hover:bg-surface-2',
              )}
            >
              <Network className="h-3.5 w-3.5" aria-hidden />
              Graph
            </button>
            <button
              type="button"
              onClick={() => setMode('list')}
              aria-pressed={mode === 'list'}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn border px-2 py-1 text-meta font-semibold',
                mode === 'list'
                  ? 'border-edge bg-surface-3 text-ink-primary'
                  : 'border-hairline text-ink-tertiary hover:bg-surface-2',
              )}
            >
              <List className="h-3.5 w-3.5" aria-hidden />
              List
            </button>
          </div>

          <button
            type="button"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
            className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                       text-meta font-semibold text-ink-secondary transition-colors duration-hover
                       hover:bg-surface-3 hover:text-ink-primary disabled:opacity-50"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', refresh.isPending && 'animate-spin')} aria-hidden />
            Refresh topology
          </button>
        </div>
      </header>

      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {refresh.isPending ? (
        <div className="mb-3">
          <WorkingIndicator label="Re-ingesting topology from telemetry…" />
        </div>
      ) : null}

      {services.isError ? (
        <div className="mb-4">
          <ErrorState
            title="Cannot load the service inventory"
            detail={(services.error as Error).message}
            consequence="The service picker is empty because Aegis is unreachable, not because no services exist."
            onRetry={() => services.refetch()}
          />
        </div>
      ) : services.data && !isAvailable(services.data) && serviceRows.length === 0 ? (
        <div className="mb-4">
          <SourceUnavailableState
            source="Service inventory"
            reason={services.data.reason}
            consequence="Services cannot be listed for the picker. Topology queries still work for a known service id."
            onRetry={() => services.refetch()}
          />
        </div>
      ) : null}

      {serviceId === '' ? (
        <EmptyState
          title="No service selected."
          detail="Choose a service to render its dependency neighbourhood."
          hint="pick a service from the control above once the inventory loads"
        />
      ) : neighbourhood.isLoading ? (
        <SkeletonRows rows={7} />
      ) : neighbourhood.isError ? (
        <ErrorState
          title="Cannot load the topology"
          detail={(neighbourhood.error as Error).message}
          consequence="No dependency information is being shown. This is a failure to query, not an absence of dependencies."
          onRetry={() => neighbourhood.refetch()}
        />
      ) : neighbourhood.data && !isAvailable(neighbourhood.data) ? (
        <div className="space-y-4">
          <SourceUnavailableState
            source="Service graph"
            reason={neighbourhood.data.reason}
            consequence="Aegis cannot draw dependencies. Blast radius, causal paths and dependency-aware risk are degraded until the topology store answers again."
            onRetry={() => neighbourhood.refetch()}
          />
          <section aria-labelledby="inventory-fallback" className="card p-4">
            <h2 id="inventory-fallback" className="text-body font-semibold text-ink-primary">
              Service inventory
            </h2>
            <p className="mt-1 text-meta font-medium text-ink-secondary">
              Read from the system of record, which is still answering. It lists what exists; it
              cannot tell you what calls what.
            </p>
            {serviceRows.length === 0 ? (
              <p className="mt-3 text-meta font-medium text-ink-tertiary">
                The inventory is empty as well.
              </p>
            ) : (
              <ul className="mt-3 grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
                {serviceRows.map((row) => (
                  <li
                    key={row.service_id}
                    className="flex items-center justify-between gap-2 rounded border border-hairline
                               bg-surface-2 px-2 py-1.5"
                  >
                    <span className="truncate text-meta font-semibold text-ink-primary">
                      {row.name}
                    </span>
                    <span className="shrink-0 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
                      {/* Graph-sourced rows carry no runtime health: say where they came from. */}
                      {row.health ?? 'from topology'}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      ) : layout && layout.nodes.length === 0 ? (
        <EmptyState
          title="The topology store holds no nodes for this service."
          detail="The graph answered successfully and returned nothing, which means ingestion has not recorded this service yet."
          hint="run Refresh topology, or widen the depth"
        />
      ) : layout ? (
        <div
          className={cn(
            'grid gap-4',
            selectedNode ? 'lg:grid-cols-[minmax(0,1fr)_340px]' : 'grid-cols-1',
          )}
        >
          <div className="min-w-0">
            <div className="mb-2.5">
              <HealthLegend hasSelection={selectedId !== null && !radiusUnknown} />
            </div>
            {radiusPending ? (
              <div className="mb-2.5">
                <WorkingIndicator label="Computing blast radius for the selection…" />
              </div>
            ) : null}
            {radiusUnknown ? (
              <div className="mb-2.5">
                {radius.isError ? (
                  radius.error instanceof NetworkError ? (
                    <SourceUnavailableState
                      source="Blast radius"
                      reason={radius.error.message}
                      consequence="No node is ringed on the canvas because the query never reached Aegis. That is an unanswered question, not an empty blast radius — treat any action on this selection as higher risk."
                      onRetry={() => radius.refetch()}
                    />
                  ) : (
                    <ErrorState
                      title="Blast radius not drawn"
                      detail={(radius.error as Error).message}
                      consequence="No node is ringed on the canvas because the query failed, not because nothing downstream is affected. Treat any action on this selection as higher risk."
                      onRetry={() => radius.refetch()}
                    />
                  )
                ) : radius.data && !isAvailable(radius.data) ? (
                  <SourceUnavailableState
                    source="Blast radius"
                    reason={radius.data.reason}
                    consequence="Aegis cannot say what fails with this service, so nothing is highlighted. An unringed canvas here means unknown, not safe."
                    onRetry={() => radius.refetch()}
                  />
                ) : null}
              </div>
            ) : null}
            {mode === 'graph' ? (
              <ForceGraph
                layout={layout}
                rootId={serviceId}
                selectedId={selectedId}
                blastRadius={blastRadius}
                onSelect={select}
              />
            ) : (
              <TopologyList
                layout={layout}
                rootId={serviceId}
                selectedId={selectedId}
                onSelect={select}
              />
            )}
          </div>
          {selectedNode ? (
            <NodeDetailPanel node={selectedNode} onClose={() => select(null)} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * `useSearchParams` opts the route into dynamic rendering, and Next refuses to
 * prerender it unless the reader sits under a Suspense boundary. The boundary
 * lives here rather than inside the console so the whole page, not a fragment
 * of it, has a defined shape while the params resolve.
 */
export default function GraphPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-[1600px] px-6 py-7">
          <SkeletonRows rows={7} />
        </div>
      }
    >
      <GraphConsole />
    </Suspense>
  );
}
