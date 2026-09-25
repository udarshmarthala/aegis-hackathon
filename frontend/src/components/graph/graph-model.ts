import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import type { GraphEdge, GraphNode } from '@/lib/console-types';

/**
 * Layout maths for the service graph.
 *
 * Kept free of JSX so the expensive part - the force simulation - is a pure
 * function of its inputs and can be run once, off the render path, and thrown
 * away. Nothing here holds a timer: the simulation is ticked to convergence a
 * bounded number of times and then stopped, so a graph left open on a wall
 * display does not keep a browser core busy for a week.
 */

export const GRAPH_WIDTH = 1080;
export const GRAPH_HEIGHT = 660;

/**
 * Rendering cap. The API already clamps to 400 nodes, but 400 nodes on one
 * canvas is an ink blot, not a dependency map. Anything dropped is reported to
 * the operator rather than silently omitted.
 */
export const MAX_RENDERED_NODES = 120;

/** Convergence bound. d3's own recommended static-layout tick count. */
const MAX_TICKS = 320;

export type NodeHealth = 'healthy' | 'degraded' | 'critical' | 'unknown';

export interface PositionedNode {
  id: string;
  name: string;
  kind: string;
  health: NodeHealth;
  environment: string | null;
  hops: number | null;
  degree: number;
  x: number;
  y: number;
  r: number;
}

export interface PositionedEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  callCount: number | null;
  errorCount: number | null;
  latencyP99Ms: number | null;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface GraphLayout {
  nodes: PositionedNode[];
  edges: PositionedEdge[];
  byId: Map<string, PositionedNode>;
  adjacency: Map<string, string[]>;
  renderedNodes: number;
  totalNodes: number;
  hiddenEdges: number;
}

function readString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function readNumber(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function healthOf(node: Record<string, unknown>): NodeHealth {
  const raw = (readString(node, 'health') ?? '').toLowerCase();
  if (raw === 'healthy' || raw === 'degraded' || raw === 'critical') return raw;
  return 'unknown';
}

export function displayName(node: GraphNode): string {
  return readString(node, 'name') ?? readString(node, 'label') ?? node.id;
}

export function nodeKind(node: GraphNode): string {
  return readString(node, 'kind') ?? readString(node, 'label') ?? 'Node';
}

function radiusFor(degree: number, isRoot: boolean): number {
  return (isRoot ? 15 : 9) + Math.min(9, degree * 1.4);
}

interface SimNode extends SimulationNodeDatum {
  id: string;
  name: string;
  kind: string;
  health: NodeHealth;
  environment: string | null;
  hops: number | null;
  degree: number;
  r: number;
}

interface SimEdge extends SimulationLinkDatum<SimNode> {
  id: string;
  type: string;
  callCount: number | null;
  errorCount: number | null;
  latencyP99Ms: number | null;
}

/**
 * Run the simulation to convergence once and return fixed coordinates.
 *
 * A live ticking simulation is prettier for three seconds and a liability for
 * the rest of the session, so this runs the ticks synchronously and stops. The
 * result is deterministic for a given input, which also means the graph does
 * not reshuffle itself every time an unrelated query refetches.
 */
export function computeLayout(
  rawNodes: GraphNode[],
  rawEdges: GraphEdge[],
  rootId: string,
): GraphLayout {
  const degree = new Map<string, number>();
  for (const edge of rawEdges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  // Nearest first: if the graph has to be truncated, the hops closest to the
  // service the operator asked about are the ones worth keeping.
  const ordered = [...rawNodes].sort((a, b) => {
    if (a.id === rootId) return -1;
    if (b.id === rootId) return 1;
    const ha = readNumber(a, 'hops') ?? 99;
    const hb = readNumber(b, 'hops') ?? 99;
    if (ha !== hb) return ha - hb;
    const da = degree.get(b.id) ?? 0;
    const db = degree.get(a.id) ?? 0;
    if (da !== db) return da - db;
    return displayName(a).localeCompare(displayName(b));
  });

  const kept = ordered.slice(0, MAX_RENDERED_NODES);
  const keptIds = new Set(kept.map((n) => n.id));

  const simNodes: SimNode[] = kept.map((node) => ({
    id: node.id,
    name: displayName(node),
    kind: nodeKind(node),
    health: healthOf(node),
    environment: readString(node, 'environment'),
    hops: readNumber(node, 'hops'),
    degree: degree.get(node.id) ?? 0,
    r: radiusFor(degree.get(node.id) ?? 0, node.id === rootId),
  }));

  const usableEdges = rawEdges.filter((e) => keptIds.has(e.source) && keptIds.has(e.target));
  const hiddenEdges = rawEdges.length - usableEdges.length;

  const simEdges: SimEdge[] = usableEdges.map((edge, index) => ({
    id: `${edge.source}|${edge.type}|${edge.target}|${index}`,
    source: edge.source,
    target: edge.target,
    type: edge.type,
    callCount: readNumber(edge, 'call_count'),
    errorCount: readNumber(edge, 'error_count'),
    latencyP99Ms: readNumber(edge, 'latency_p99_ms'),
  }));

  const root = simNodes.find((n) => n.id === rootId);
  if (root) {
    // Pinning the requested service centres the story on it instead of letting
    // the densest cluster win the middle of the canvas.
    root.fx = GRAPH_WIDTH / 2;
    root.fy = GRAPH_HEIGHT / 2;
  }

  const simulation = forceSimulation<SimNode, SimEdge>(simNodes)
    .force(
      'link',
      forceLink<SimNode, SimEdge>(simEdges)
        .id((d) => d.id)
        .distance(120)
        .strength(0.35),
    )
    .force('charge', forceManyBody<SimNode>().strength(-460).distanceMax(720))
    .force('collide', forceCollide<SimNode>().radius((d) => d.r + 22))
    .force('center', forceCenter(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2))
    .force('x', forceX<SimNode>(GRAPH_WIDTH / 2).strength(0.035))
    .force('y', forceY<SimNode>(GRAPH_HEIGHT / 2).strength(0.055))
    .stop();

  const decay = 1 - simulation.alphaDecay();
  const needed = decay > 0 ? Math.ceil(Math.log(simulation.alphaMin()) / Math.log(decay)) : MAX_TICKS;
  const ticks = Math.max(1, Math.min(MAX_TICKS, needed));
  for (let i = 0; i < ticks; i += 1) simulation.tick();
  simulation.stop();

  const positioned = fitToCanvas(simNodes);
  const byId = new Map(positioned.map((n) => [n.id, n]));

  const edges: PositionedEdge[] = [];
  for (const edge of simEdges) {
    const sourceId = typeof edge.source === 'object' ? edge.source.id : String(edge.source);
    const targetId = typeof edge.target === 'object' ? edge.target.id : String(edge.target);
    const from = byId.get(sourceId);
    const to = byId.get(targetId);
    if (!from || !to) continue;
    edges.push({
      id: edge.id,
      source: sourceId,
      target: targetId,
      type: edge.type,
      callCount: edge.callCount,
      errorCount: edge.errorCount,
      latencyP99Ms: edge.latencyP99Ms,
      x1: from.x,
      y1: from.y,
      x2: to.x,
      y2: to.y,
    });
  }

  const adjacency = new Map<string, string[]>();
  for (const node of positioned) adjacency.set(node.id, []);
  for (const edge of edges) {
    adjacency.get(edge.source)?.push(edge.target);
    adjacency.get(edge.target)?.push(edge.source);
  }

  return {
    nodes: positioned,
    edges,
    byId,
    adjacency,
    renderedNodes: positioned.length,
    totalNodes: rawNodes.length,
    hiddenEdges,
  };
}

/** Shrink-to-fit, never grow-to-fill: node radii stay comparable across views. */
function fitToCanvas(nodes: SimNode[]): PositionedNode[] {
  const pad = 56;
  const xs = nodes.map((n) => n.x ?? GRAPH_WIDTH / 2);
  const ys = nodes.map((n) => n.y ?? GRAPH_HEIGHT / 2);
  const minX = Math.min(...xs, GRAPH_WIDTH / 2);
  const maxX = Math.max(...xs, GRAPH_WIDTH / 2);
  const minY = Math.min(...ys, GRAPH_HEIGHT / 2);
  const maxY = Math.max(...ys, GRAPH_HEIGHT / 2);
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  const scale = Math.min(1, (GRAPH_WIDTH - pad * 2) / spanX, (GRAPH_HEIGHT - pad * 2) / spanY);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;

  return nodes.map((node) => ({
    id: node.id,
    name: node.name,
    kind: node.kind,
    health: node.health,
    environment: node.environment,
    hops: node.hops,
    degree: node.degree,
    r: node.r,
    x: GRAPH_WIDTH / 2 + ((node.x ?? cx) - cx) * scale,
    y: GRAPH_HEIGHT / 2 + ((node.y ?? cy) - cy) * scale,
  }));
}

/**
 * Geometric neighbour in a compass direction, for arrow-key navigation.
 *
 * Distance alone would jump across the canvas; the perpendicular penalty keeps
 * "right" meaning right rather than "vaguely over there".
 */
export function neighbourInDirection(
  from: PositionedNode,
  candidates: PositionedNode[],
  direction: 'up' | 'down' | 'left' | 'right',
): PositionedNode | null {
  let best: PositionedNode | null = null;
  let bestScore = Number.POSITIVE_INFINITY;

  for (const node of candidates) {
    if (node.id === from.id) continue;
    const dx = node.x - from.x;
    const dy = node.y - from.y;
    const along =
      direction === 'right' ? dx : direction === 'left' ? -dx : direction === 'down' ? dy : -dy;
    if (along <= 1) continue;
    const across = direction === 'left' || direction === 'right' ? Math.abs(dy) : Math.abs(dx);
    const score = along + across * 2.5;
    if (score < bestScore) {
      bestScore = score;
      best = node;
    }
  }
  return best;
}
