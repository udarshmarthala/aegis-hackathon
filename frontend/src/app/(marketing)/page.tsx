import { Suspense } from 'react';
import type { Metadata } from 'next';
import { LandingContent } from '@/components/marketing/LandingContent';
import { SignInPanel } from '@/components/marketing/SignInPanel';

export const metadata: Metadata = {
  title: 'Aegis — AI-native SRE control plane',
  description:
    'Aegis detects, investigates, debugs, repairs, verifies and safely remediates incidents in distributed systems. Evidence before action.',
};

/**
 * The landing page.
 *
 * A fixed two-column split: the narrative scrolls on the left, the way in stays
 * put on the right. An operator arriving mid-incident should never have to
 * scroll to find the sign-in, and a first-time reader should never lose it
 * while reading.
 *
 * Below `lg` the panel un-fixes and stacks above the content, because a 30%
 * column on a phone is a 110px sliver nobody can type a password into.
 */
export default function LandingPage() {
  return (
    <div className="min-h-screen bg-canvas">
      {/* Sign-in: fixed, right, 30% of the viewport on large screens. */}
      <aside
        aria-label="Sign in"
        className="border-b border-line bg-surface-1
                   lg:fixed lg:inset-y-0 lg:right-0 lg:z-20 lg:w-[30%] lg:min-w-[380px]
                   lg:overflow-y-auto lg:border-b-0 lg:border-l"
      >
        <Suspense
          fallback={
            <div className="flex h-full items-center justify-center px-8 py-12">
              <span className="text-body font-medium text-ink-tertiary">Loading sign-in…</span>
            </div>
          }
        >
          <SignInPanel />
        </Suspense>
      </aside>

      {/* Narrative: the remaining 70%. */}
      <div className="lg:mr-[30%] lg:min-w-0">
        <LandingContent />
      </div>
    </div>
  );
}
