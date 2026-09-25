'use client';

import { useEffect } from 'react';

/**
 * Last-resort boundary, for a throw in the root layout itself.
 *
 * This replaces the whole document, so it must supply its own <html> and
 * <body> - the layout that would normally provide them is the thing that
 * failed. For the same reason it carries inline styles rather than Tailwind
 * classes: a stylesheet that did not load is one of the ways to arrive here,
 * and a boundary that depends on the broken thing is not a boundary.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('Console root layout failed', {
      message: error.message,
      digest: error.digest,
    });
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#000',
          color: '#e5e5e5',
          fontFamily: 'ui-sans-serif, system-ui, sans-serif',
          padding: '24px',
        }}
      >
        <div role="alert" style={{ maxWidth: '36rem' }}>
          <h1 style={{ fontSize: '1rem', fontWeight: 600, margin: '0 0 8px' }}>
            The Aegis console failed to start.
          </h1>
          <p style={{ fontSize: '0.8125rem', lineHeight: 1.6, margin: '0 0 8px', color: '#a3a3a3' }}>
            {error.message || 'The failure did not carry a message.'}
          </p>
          <p style={{ fontSize: '0.8125rem', lineHeight: 1.6, margin: '0 0 16px', color: '#737373' }}>
            This is a fault in the console itself. It says nothing about the health of the systems
            Aegis is watching, and no incident state has been changed.
            {error.digest ? ` Reference ${error.digest} when reporting this.` : ''}
          </p>
          <button
            type="button"
            onClick={reset}
            style={{
              background: 'transparent',
              border: '1px solid #404040',
              borderRadius: '6px',
              color: '#e5e5e5',
              cursor: 'pointer',
              fontSize: '0.8125rem',
              padding: '6px 12px',
            }}
          >
            Reload the console
          </button>
        </div>
      </body>
    </html>
  );
}
