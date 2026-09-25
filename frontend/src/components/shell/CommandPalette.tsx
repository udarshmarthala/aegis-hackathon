'use client';

import { Command } from 'cmdk';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PlugZap, RefreshCw } from 'lucide-react';
import { ApiError, NetworkError, api } from '@/lib/api';
import { SeverityBadge } from '@/components/ui/primitives';
import { WorkingIndicator } from '@/components/ui/states';
import { NAV_DESTINATIONS } from './navigation';

/** Unreachable and refused are different problems with different next steps. */
function incidentSearchReason(error: unknown): string {
  if (error instanceof NetworkError) return error.message;
  if (error instanceof ApiError) return `Aegis answered ${error.status} (${error.code}).`;
  return 'The incident list could not be read.';
}

/**
 * Fuzzy command palette (UX spec 9). Ctrl/Cmd+K from anywhere.
 *
 * Incidents are fetched only while the palette is open, so the shortcut costs
 * nothing until it is used.
 */
export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['palette-incidents'],
    queryFn: () => api.listIncidents({ limit: 25 }),
    enabled: open,
    staleTime: 15_000,
  });

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'k' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        onOpenChange(!open);
      }
      if (event.key === 'Escape') onOpenChange(false);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onOpenChange]);

  function go(href: string) {
    onOpenChange(false);
    router.push(href);
  }

  return (
    <Command.Dialog
      open={open}
      onOpenChange={onOpenChange}
      label="Command palette"
      className="fixed inset-0 z-50"
    >
      {/* A click-to-dismiss backdrop, deliberately left as a div: Escape above
          already provides the keyboard path, and making it a real button would
          put an aria-hidden element into the focus order. */}
      <div
        className="fixed inset-0 bg-black/70 backdrop-blur-[2px]"
        onClick={() => onOpenChange(false)}
        aria-hidden
      />
      <div
        className="fixed left-1/2 top-[18%] w-[min(620px,92vw)] -translate-x-1/2
                   overflow-hidden rounded-drawer border border-edge bg-surface-1
                   shadow-2xl shadow-black/60"
      >
        <Command.Input
          autoFocus
          placeholder="Search incidents, services, actions…"
          className="w-full border-b border-hairline bg-transparent px-4 py-3
                     text-body text-ink-primary outline-none placeholder:text-ink-tertiary"
        />
        <Command.List className="max-h-[52vh] overflow-y-auto p-2">
          <Command.Empty className="px-3 py-6 text-center text-meta text-ink-tertiary">
            Nothing matches that. Try an incident id, a service name, or a page.
          </Command.Empty>

          {isLoading ? (
            <div className="px-2.5 py-2">
              <WorkingIndicator label="Searching incidents" />
            </div>
          ) : null}

          {/* Without this the palette silently degrades to a link list, and an
              operator searching for a live incident is told, in effect, that
              there is none. */}
          {isError ? (
            <div
              role="alert"
              className="mx-1 mb-2 flex items-start gap-2 rounded-btn border border-status-warning/30
                         bg-status-warning/5 px-2.5 py-2"
            >
              <PlugZap className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-warning" aria-hidden />
              <div className="space-y-1">
                <p className="text-meta text-status-warning">Incident search is unavailable.</p>
                <p className="text-meta text-ink-tertiary">
                  {incidentSearchReason(error)} Only the destinations below are searchable; this is
                  not a report that there are no incidents.
                </p>
                <button
                  type="button"
                  onClick={() => void refetch()}
                  className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2 py-0.5
                             text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
                >
                  <RefreshCw className="h-3 w-3" aria-hidden />
                  Retry incident search
                </button>
              </div>
            </div>
          ) : null}

          {data?.items.length ? (
            <Command.Group
              heading="Incidents"
              className="[&_[cmdk-group-heading]]:label-meta [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1"
            >
              {data.items.map((incident) => (
                <Command.Item
                  key={incident.id}
                  value={`${incident.id} ${incident.title} ${incident.affected_services.join(' ')}`}
                  onSelect={() => go(`/incidents/${incident.id}`)}
                  className="flex cursor-pointer items-center gap-2.5 rounded-btn px-2.5 py-2
                             text-body text-ink-secondary
                             data-[selected=true]:bg-surface-3 data-[selected=true]:text-ink-primary"
                >
                  <SeverityBadge severity={incident.severity} />
                  <span className="truncate">{incident.title}</span>
                  <span className="ml-auto shrink-0 font-mono text-meta text-ink-tertiary">
                    {incident.state}
                  </span>
                </Command.Item>
              ))}
            </Command.Group>
          ) : null}

          {/* Every primary destination, from the one list the sidebar also
              reads, so the palette is a complete alternative to it rather than
              a partial one. */}
          <Command.Group
            heading="Go to"
            className="[&_[cmdk-group-heading]]:label-meta [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1"
          >
            {NAV_DESTINATIONS.map(({ href, label, icon: Icon, keywords }) => (
              <Command.Item
                key={href}
                value={keywords ? `${label} ${keywords}` : label}
                onSelect={() => go(href)}
                className="flex cursor-pointer items-center gap-2.5 rounded-btn px-2.5 py-2
                           text-body text-ink-secondary
                           data-[selected=true]:bg-surface-3 data-[selected=true]:text-ink-primary"
              >
                <Icon className="h-[15px] w-[15px] shrink-0" aria-hidden />
                {label}
              </Command.Item>
            ))}
          </Command.Group>
        </Command.List>
      </div>
    </Command.Dialog>
  );
}
