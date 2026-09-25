import {
  Activity, AlertCircle, BarChart3, Boxes, Bug, CheckSquare, FileSearch,
  GitBranch, Network, ScrollText, Settings, ShieldCheck, Siren, Workflow,
  type LucideIcon,
} from 'lucide-react';

/**
 * The single source of truth for primary navigation, read by both the sidebar
 * and the command palette.
 *
 * It exists because those two surfaces each kept their own hand-written copy,
 * and the copies drifted: the palette silently fell eight destinations behind
 * the sidebar, so an operator who reached for Ctrl+K and could not find a page
 * reasonably concluded it did not exist. One list cannot drift from itself.
 *
 * Grouping follows operator goal, not implementation (UX spec 7). The word
 * "AI" deliberately does not dominate: AI is the engine inside the product,
 * not a destination.
 */

/**
 * Enumerated rather than `string` so a typo in an href fails at compile time
 * instead of shipping a link to a 404.
 */
export type NavHref =
  | '/overview'
  | '/incidents'
  | '/systems'
  | '/tasks'
  | '/graph'
  | '/reliability'
  | '/recurring'
  | '/investigations'
  | '/debug'
  | '/deployments'
  | '/approvals'
  | '/evaluation'
  | '/audit'
  | '/policies'
  | '/settings';

export type NavDestination = {
  readonly href: NavHref;
  readonly label: string;
  readonly icon: LucideIcon;
  /**
   * Extra terms the palette matches on, for the words an operator actually
   * types. Somebody hunting the kill switch searches "kill switch", not
   * "Policies". The sidebar ignores this field.
   */
  readonly keywords?: string;
};

export type NavSection = {
  readonly label: string;
  readonly items: readonly NavDestination[];
};

export const NAV_SECTIONS: readonly NavSection[] = [
  {
    label: 'Command',
    items: [
      { href: '/overview', label: 'Home', icon: Activity, keywords: 'overview command centre' },
      { href: '/incidents', label: 'Incidents', icon: Siren },
      { href: '/systems', label: 'Live Systems', icon: Boxes, keywords: 'services topology' },
      { href: '/tasks', label: 'My Tasks', icon: CheckSquare },
    ],
  },
  {
    label: 'Reliability',
    items: [
      { href: '/graph', label: 'Service Graph', icon: Network, keywords: 'topology dependencies' },
      { href: '/reliability', label: 'Reliability', icon: BarChart3, keywords: 'slo error budget' },
      { href: '/recurring', label: 'Recurring Failures', icon: Workflow, keywords: 'patterns repeat' },
    ],
  },
  {
    label: 'Engineering',
    items: [
      { href: '/investigations', label: 'Investigations', icon: FileSearch },
      { href: '/debug', label: 'Debug Workbench', icon: Bug, keywords: 'patch repair' },
      { href: '/deployments', label: 'Deployments', icon: GitBranch, keywords: 'releases changes' },
    ],
  },
  {
    label: 'Governance',
    items: [
      { href: '/approvals', label: 'Approvals', icon: ShieldCheck },
      { href: '/evaluation', label: 'AI Evaluation', icon: BarChart3, keywords: 'benchmark' },
      { href: '/audit', label: 'Audit', icon: ScrollText, keywords: 'audit log' },
      { href: '/policies', label: 'Policies', icon: AlertCircle, keywords: 'kill switch autonomy' },
    ],
  },
  {
    label: 'Configuration',
    items: [{ href: '/settings', label: 'Settings', icon: Settings, keywords: 'integration health' }],
  },
] as const;

/**
 * The same destinations flattened in sidebar order, for surfaces that present
 * one flat list. Derived rather than declared, so it cannot fall behind.
 */
export const NAV_DESTINATIONS: readonly NavDestination[] = NAV_SECTIONS.flatMap(
  (section) => section.items,
);
