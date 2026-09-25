'use client';

import { useEffect } from 'react';
import Link from 'next/link';
import { AlertTriangle, RefreshCw } from 'lucide-react';

/**
 * Route-level error boundary.
 *
 * Without one, any render throw falls through to the framework's default error
 * page, which tells an operator nothing and offers no way back. The console is
 * looked at during incidents; a dead end here costs minutes at the worst
 * possible time.
 *
 * Note what this deliberately does not claim: a render failure in the console
 * says nothing about the health of the systems being watched. Saying so in as
 * many words stops an operator reading a broken page as a broken platform.
 */
export default function ConsoleRouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The digest is the only handle on the server-side stack, which is withheld
    // from the browser in production. Losing it means the report an operator
    // files cannot be tied to anything.
    console.error('Console render failed', { message: error.message, digest: error.digest });
  }, [error]);

  return (
    <main id="main" className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-16">
      <div className="card border-status-critical/30 p-6" role="alert">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-status-critical" aria-hidden />
          <div className="space-y-3">
            <div className="space-y-1.5">
              <h1 className="text-body font-medium text-status-critical">
                This page of the console failed to render.
              </h1>
              <p className="text-meta text-ink-secondary">
                {error.message || 'The failure did not carry a message.'}
              </p>
              <p className="text-meta text-ink-tertiary">
                This is a fault in the console itself. It says nothing about the health of the
                systems Aegis is watching, and no incident state has been changed.
              </p>
              {error.digest ? (
                <p className="text-meta text-ink-tertiary">
                  Reference <code className="font-mono">{error.digest}</code> when reporting this.
                </p>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={reset}
                className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                           text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
              >
                <RefreshCw className="h-3 w-3" aria-hidden />
                Try this page again
              </button>
              <Link
                href="/overview"
                className="inline-flex items-center rounded-btn border border-line px-2.5 py-1.5
                           text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
              >
                Back to overview
              </Link>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
