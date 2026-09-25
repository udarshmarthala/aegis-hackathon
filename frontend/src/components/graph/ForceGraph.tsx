'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  GRAPH_HEIGHT,
  GRAPH_WIDTH,
  neighbourInDirection,
  type GraphLayout,
  type NodeHealth,
  type PositionedNode,
} from './graph-model';

/**
 * The service graph canvas.
 *
 * Health is never carried by colour alone: every node has its name in the
 * canvas, its health in the accessible name, and a distinct outline treatment
 * per state, so the graph still reads on a washed-out monitor at 2am.
 *
 * Keyboard operation is not an afterthought here. One tab stop puts focus on a
 * node; arrow keys walk the topology geometrically; Enter selects. An operator
 * who cannot use a mouse gets the same map, not a downgraded one.
 */

const NODE_FILL: Record<NodeHealth, string> = {
  healthy: 'fill-status-success/25',
  degraded: 'fill-status-warning/25',
  critical: 'fill-status-critical/30',
  unknown: 'fill-status-neutral/20',
};

const NODE_STROKE: Record<NodeHealth, string> = {
  healthy: 'stroke-status-success',
  degraded: 'stroke-status-warning',
  critical: 'stroke-status-critical',
  unknown: 'stroke-status-neutral',
};

/** Outline shape, so health survives greyscale and colour-blind vision. */
const NODE_DASH: Record<NodeHealth, string | undefined> = {
  healthy: undefined,
  degraded: '5 3',
  critical: undefined,
  unknown: '1 3',
};

const HEALTH_WORD: Record<NodeHealth, string> = {
  healthy: 'healthy',
  degraded: 'degraded',
  critical: 'critical',
  unknown: 'health unknown',
};

const MIN_ZOOM = 0.4;
const MAX_ZOOM = 4;
const EDGE_LABEL_BUDGET = 26;

interface View {
  x: number;
  y: number;
  k: number;
}

const INITIAL_VIEW: View = { x: 0, y: 0, k: 1 };

export interface ForceGraphProps {
  layout: GraphLayout;
  rootId: string;
  selectedId: string | null;
  /** Services that fail with the selection. Empty when the radius is unknown. */
  blastRadius: ReadonlySet<string>;
  onSelect: (id: string | null) => void;
}

export function ForceGraph({
  layout,
  rootId,
  selectedId,
  blastRadius,
  onSelect,
}: ForceGraphProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const nodeRefs = useRef(new Map<string, SVGGElement>());
  const [view, setView] = useState<View>(INITIAL_VIEW);
  const [focusedId, setFocusedId] = useState<string>(rootId);
  const dragRef = useRef<{ pointerId: number; x: number; y: number } | null>(null);

  const nodes = layout.nodes;
  const fallbackFocus = nodes[0]?.id ?? rootId;

  useEffect(() => {
    // A refetch that drops the focused node must not leave focus nowhere.
    if (!layout.byId.has(focusedId)) setFocusedId(layout.byId.has(rootId) ? rootId : fallbackFocus);
  }, [layout, focusedId, rootId, fallbackFocus]);

  const neighbourIds = useMemo(() => {
    if (!selectedId) return new Set<string>();
    return new Set(layout.adjacency.get(selectedId) ?? []);
  }, [layout, selectedId]);

  const zoomAt = useCallback((anchorX: number, anchorY: number, factor: number) => {
    setView((prev) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev.k * factor));
      if (k === prev.k) return prev;
      return {
        k,
        x: anchorX - ((anchorX - prev.x) * k) / prev.k,
        y: anchorY - ((anchorY - prev.y) * k) / prev.k,
      };
    });
  }, []);

  // React attaches wheel passively at the root, so the listener is registered
  // directly to keep the page from scrolling while the operator zooms.
  useEffect(() => {
    const element = svgRef.current;
    if (!element) return undefined;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      const px = ((event.clientX - rect.left) / rect.width) * GRAPH_WIDTH;
      const py = ((event.clientY - rect.top) / rect.height) * GRAPH_HEIGHT;
      zoomAt(px, py, Math.exp(-event.deltaY * 0.0015));
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  const ensureVisible = useCallback((node: PositionedNode) => {
    setView((prev) => {
      const sx = node.x * prev.k + prev.x;
      const sy = node.y * prev.k + prev.y;
      const margin = 80;
      const inside =
        sx > margin && sx < GRAPH_WIDTH - margin && sy > margin && sy < GRAPH_HEIGHT - margin;
      if (inside) return prev;
      return {
        k: prev.k,
        x: GRAPH_WIDTH / 2 - node.x * prev.k,
        y: GRAPH_HEIGHT / 2 - node.y * prev.k,
      };
    });
  }, []);

  const focusNode = useCallback(
    (node: PositionedNode | null) => {
      if (!node) return;
      setFocusedId(node.id);
      nodeRefs.current.get(node.id)?.focus();
      ensureVisible(node);
    },
    [ensureVisible],
  );

  function handleKeyDown(event: React.KeyboardEvent<SVGSVGElement>) {
    const current = layout.byId.get(focusedId) ?? nodes[0];
    if (!current) return;

    switch (event.key) {
      case 'ArrowUp':
      case 'ArrowDown':
      case 'ArrowLeft':
      case 'ArrowRight': {
        event.preventDefault();
        const direction =
          event.key === 'ArrowUp'
            ? 'up'
            : event.key === 'ArrowDown'
              ? 'down'
              : event.key === 'ArrowLeft'
                ? 'left'
                : 'right';
        focusNode(neighbourInDirection(current, nodes, direction));
        break;
      }
      case 'Enter':
      case ' ':
        event.preventDefault();
        onSelect(current.id);
        break;
      case 'Escape':
        onSelect(null);
        break;
      case 'Home':
        event.preventDefault();
        focusNode(layout.byId.get(rootId) ?? nodes[0] ?? null);
        break;
      case '+':
      case '=':
        event.preventDefault();
        zoomAt(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2, 1.25);
        break;
      case '-':
        event.preventDefault();
        zoomAt(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2, 0.8);
        break;
      case '0':
        event.preventDefault();
        setView(INITIAL_VIEW);
        break;
      default:
        break;
    }
  }

  function handlePointerDown(event: React.PointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    dragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: React.PointerEvent<SVGSVGElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const dx = ((event.clientX - drag.x) / rect.width) * GRAPH_WIDTH;
    const dy = ((event.clientY - drag.y) / rect.height) * GRAPH_HEIGHT;
    dragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
    setView((prev) => ({ ...prev, x: prev.x + dx, y: prev.y + dy }));
  }

  function handlePointerUp(event: React.PointerEvent<SVGSVGElement>) {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  const showAllEdgeLabels = layout.edges.length <= EDGE_LABEL_BUDGET;

  return (
    <div className="relative">
      <div className="absolute right-3 top-3 z-10 flex flex-col gap-1">
        <ZoomButton label="Zoom in" onClick={() => zoomAt(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2, 1.25)}>
          <ZoomIn className="h-3.5 w-3.5" aria-hidden />
        </ZoomButton>
        <ZoomButton label="Zoom out" onClick={() => zoomAt(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2, 0.8)}>
          <ZoomOut className="h-3.5 w-3.5" aria-hidden />
        </ZoomButton>
        <ZoomButton label="Reset view" onClick={() => setView(INITIAL_VIEW)}>
          <Maximize2 className="h-3.5 w-3.5" aria-hidden />
        </ZoomButton>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`}
        className="h-[560px] w-full touch-none select-none rounded-card bg-surface-1"
        role="group"
        aria-label="Service dependency graph. Tab to enter, arrow keys to move between services, Enter to select."
        onKeyDown={handleKeyDown}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
      >
        <title>Service dependency graph</title>
        <desc>
          {`${layout.renderedNodes} services and ${layout.edges.length} dependency edges around ${rootId}. The same topology is available as a readable list from the List view control.`}
        </desc>
        <defs>
          <marker id="aegis-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3"
                  orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L6,3 L0,6 Z" className="fill-edge" />
          </marker>
          <marker id="aegis-arrow-active" markerWidth="7" markerHeight="7" refX="6" refY="3"
                  orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L6,3 L0,6 Z" className="fill-accent" />
          </marker>
          <marker id="aegis-arrow-dim" markerWidth="7" markerHeight="7" refX="6" refY="3"
                  orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L6,3 L0,6 Z" className="fill-hairline" />
          </marker>
        </defs>

        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          <g aria-hidden>
            {layout.edges.map((edge) => {
              const from = layout.byId.get(edge.source);
              const to = layout.byId.get(edge.target);
              if (!from || !to) return null;
              const dx = to.x - from.x;
              const dy = to.y - from.y;
              const len = Math.hypot(dx, dy) || 1;
              const ux = dx / len;
              const uy = dy / len;
              const x1 = from.x + ux * (from.r + 2);
              const y1 = from.y + uy * (from.r + 2);
              const x2 = to.x - ux * (to.r + 10);
              const y2 = to.y - uy * (to.r + 10);
              const active =
                selectedId !== null && (edge.source === selectedId || edge.target === selectedId);
              const dimmed = selectedId !== null && !active;

              return (
                <g key={edge.id}>
                  <line
                    x1={x1}
                    y1={y1}
                    x2={x2}
                    y2={y2}
                    strokeWidth={active ? 1.6 : 1}
                    className={cn(active ? 'stroke-accent' : dimmed ? 'stroke-hairline' : 'stroke-edge')}
                    markerEnd={`url(#${active ? 'aegis-arrow-active' : dimmed ? 'aegis-arrow-dim' : 'aegis-arrow'})`}
                  />
                  {active || (showAllEdgeLabels && !dimmed) ? (
                    <text
                      x={(x1 + x2) / 2}
                      y={(y1 + y2) / 2 - 4}
                      textAnchor="middle"
                      className={cn(
                        'text-[8px] font-semibold uppercase tracking-wider',
                        active ? 'fill-accent' : 'fill-ink-tertiary',
                      )}
                    >
                      {edge.type}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>

          {nodes.map((node) => {
            const isSelected = node.id === selectedId;
            const inRadius = blastRadius.has(node.id);
            const isNeighbour = neighbourIds.has(node.id);
            const dimmed = selectedId !== null && !isSelected && !inRadius && !isNeighbour;
            const isRoot = node.id === rootId;
            return (
              <g
                key={node.id}
                ref={(element) => {
                  if (element) nodeRefs.current.set(node.id, element);
                  else nodeRefs.current.delete(node.id);
                }}
                role="button"
                aria-pressed={isSelected}
                aria-label={`${node.name}. ${node.kind}. ${HEALTH_WORD[node.health]}. ${node.degree} connections.${isRoot ? ' Graph root.' : ''}${inRadius ? ' In the blast radius of the selected service.' : ''}`}
                tabIndex={node.id === focusedId ? 0 : -1}
                className={cn('cursor-pointer', dimmed && 'opacity-30')}
                onFocus={() => setFocusedId(node.id)}
                onPointerDown={(event) => event.stopPropagation()}
                onClick={() => onSelect(node.id)}
              >
                <title>{`${node.name} — ${HEALTH_WORD[node.health]}`}</title>
                {inRadius ? (
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.r + 7}
                    strokeDasharray="3 3"
                    strokeWidth={1.2}
                    className="fill-none stroke-status-warning"
                  />
                ) : null}
                {node.id === focusedId ? (
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.r + 11}
                    strokeWidth={1.4}
                    className="fill-none stroke-accent"
                  />
                ) : null}
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={node.r}
                  strokeWidth={isSelected ? 3 : node.health === 'critical' ? 2.4 : 1.4}
                  strokeDasharray={NODE_DASH[node.health]}
                  className={cn(NODE_FILL[node.health], NODE_STROKE[node.health])}
                />
                {isRoot ? (
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={Math.max(2, node.r - 5)}
                    className={cn('fill-none', NODE_STROKE[node.health])}
                    strokeWidth={1}
                  />
                ) : null}
                <text
                  x={node.x}
                  y={node.y + node.r + 13}
                  textAnchor="middle"
                  className={cn(
                    'text-[10px] font-semibold',
                    isSelected ? 'fill-ink-primary' : 'fill-ink-secondary',
                  )}
                >
                  {node.name.length > 22 ? `${node.name.slice(0, 21)}…` : node.name}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      <p className="mt-2 px-1 text-meta font-medium text-ink-tertiary">
        Drag to pan, scroll to zoom. Keyboard: Tab into the graph, arrow keys move between
        services, Enter selects, Escape clears, Home returns to the root, plus and minus zoom.
      </p>
      {layout.renderedNodes < layout.totalNodes ? (
        <p className="mt-1 px-1 text-meta font-semibold text-status-warning">
          Showing the {layout.renderedNodes} nearest of {layout.totalNodes} nodes
          {layout.hiddenEdges > 0
            ? ` and hiding ${layout.hiddenEdges} edges that left the view`
            : ''}
          . Reduce the depth for a complete picture of a smaller neighbourhood.
        </p>
      ) : null}
    </div>
  );
}

function ZoomButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="rounded-btn border border-line bg-surface-2 p-1.5 text-ink-secondary
                 transition-colors duration-hover hover:bg-surface-3 hover:text-ink-primary"
    >
      {children}
    </button>
  );
}
