'use client';

import { Bot, Cpu, UserCheck } from 'lucide-react';
import type { AuditEntry } from '@/lib/console-types';
import { cn } from '@/lib/utils';

/**
 * Who did this.
 *
 * The question the audit trail exists to answer is "did a person authorise
 * this?", so a human actor is given a visibly different treatment from an agent
 * or the system - a filled badge and an accent rule down the row, not a
 * different shade of grey. Colour is never the only cue: each badge carries its
 * icon and the word.
 */

export type ActorType = AuditEntry['actor_type'];

const ACTOR = {
  human: {
    label: 'Human',
    icon: UserCheck,
    badge: 'border-accent/60 bg-accent/15 text-accent',
    rule: 'border-l-accent',
  },
  agent: {
    label: 'Agent',
    icon: Bot,
    badge: 'border-hairline bg-surface-3 text-ink-secondary',
    rule: 'border-l-edge',
  },
  system: {
    label: 'System',
    icon: Cpu,
    badge: 'border-hairline bg-surface-2 text-ink-tertiary',
    rule: 'border-l-hairline',
  },
} as const;

function entryFor(actorType: string) {
  return ACTOR[actorType as ActorType] ?? ACTOR.system;
}

export function ActorBadge({ actorType, actor }: { actorType: string; actor: string }) {
  const tone = entryFor(actorType);
  const Icon = tone.icon;
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span
        className={cn(
          'inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5',
          'text-meta font-bold uppercase tracking-wider',
          tone.badge,
        )}
      >
        <Icon className="h-3 w-3" aria-hidden />
        {tone.label}
      </span>
      <span
        className={cn(
          'min-w-0 truncate font-mono text-meta',
          actorType === 'human' ? 'font-bold text-ink-primary' : 'font-medium text-ink-tertiary',
        )}
        title={actor}
      >
        {actor}
      </span>
    </span>
  );
}

/** The left rule that makes a human decision findable while scrolling fast. */
export function actorRule(actorType: string): string {
  return entryFor(actorType).rule;
}

export function isHuman(actorType: string): boolean {
  return actorType === 'human';
}
