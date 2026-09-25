import Link from 'next/link';
import { Compass } from 'lucide-react';

/**
 * 404.
 *
 * Worth writing rather than inheriting the default, because the most likely way
 * to reach it is a stale or mistyped incident link pasted into a chat thread
 * during an incident. That reader needs a route onward, not a bare status code.
 */
export default function NotFound() {
  return (
    <main id="main" className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-16">
      <div className="card p-6">
        <div className="flex items-start gap-3">
          <Compass className="mt-0.5 h-5 w-5 shrink-0 text-ink-tertiary" aria-hidden />
          <div className="space-y-3">
            <div className="space-y-1.5">
              <h1 className="text-body font-medium text-ink-primary">
                There is nothing at this address.
              </h1>
              <p className="text-meta text-ink-secondary">
                The page does not exist. If you followed a link to an incident, the identifier may
                be mistyped - an incident that exists but you cannot see would say so instead.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href="/overview"
                className="inline-flex items-center rounded-btn border border-line px-2.5 py-1.5
                           text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
              >
                Go to overview
              </Link>
              <Link
                href="/incidents"
                className="inline-flex items-center rounded-btn border border-line px-2.5 py-1.5
                           text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
              >
                Browse incidents
              </Link>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
