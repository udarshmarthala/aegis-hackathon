import type { Metadata, Viewport } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';
import { AuthProvider } from '@/components/auth/AuthProvider';

// Self-hosted via next/font so the console does not depend on a third-party
// CDN being reachable during an incident - the one moment it must load.
const inter = Inter({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-sans',
});

export const metadata: Metadata = {
  title: {
    default: 'Aegis',
    template: '%s · Aegis',
  },
  description:
    'Evidence-driven AI SRE control plane. Detect, investigate, verify and safely remediate production incidents.',
  icons: { icon: '/favicon.svg' },
};

export const viewport: Viewport = {
  themeColor: '#000000',
  width: 'device-width',
  initialScale: 1,
};

/**
 * Root layout.
 *
 * Deliberately thin: it owns the document, the font and the session, and
 * nothing else. The console chrome lives in the (console) route group so the
 * landing page can render without a sidebar, and so an unauthenticated visitor
 * never downloads the operator console at all.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <body className="bg-canvas text-ink-primary antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50
                     focus:rounded-btn focus:bg-surface-3 focus:px-3 focus:py-2 focus:text-body
                     focus:font-semibold"
        >
          Skip to content
        </a>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
