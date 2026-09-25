import { AegisMark } from '@/components/shell/AegisMark';
import { FlowTimeline } from './FlowTimeline';
import { CAPABILITIES, GUARANTEES, PIPELINE } from './content';

/**
 * The public narrative.
 *
 * A server component with no client JavaScript: it is static prose, and an
 * operator on a degraded network during an incident should not wait on a bundle
 * to read what this system does.
 *
 * The tone is deliberately flat. This is infrastructure that can change
 * production, and overselling it on the way in would be the wrong first
 * impression for the audience that has to trust it at 3am.
 */
export function LandingContent() {
  return (
    <div className="relative">
      <GridTexture />

      <div className="relative mx-auto w-full max-w-[860px] px-6 sm:px-10 lg:px-14">
        <LandingHeader />
        <Hero />

        <section id="lifecycle" className="scroll-mt-8 border-t border-hairline py-16">
          <SectionHeading
            eyebrow="Operational reasoning"
            title="Watch the incident become understandable."
            body="Every conclusion is tied to telemetry, topology, recent change, code, prior incidents and verification results. Nothing is asserted that cannot be traced back to something observed."
          />
          <div className="mt-9">
            <FlowTimeline />
          </div>
        </section>

        <Capabilities />
        <Guarantees />
        <LandingFooter />
      </div>
    </div>
  );
}

/** A faint grid, masked out before it reaches the text. Texture, not decoration. */
function GridTexture() {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 opacity-[0.35]
                 [background-image:linear-gradient(rgba(255,255,255,.022)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.022)_1px,transparent_1px)]
                 [background-size:48px_48px]
                 [mask-image:linear-gradient(to_bottom,rgba(0,0,0,.9),transparent_75%)]"
    />
  );
}

function LandingHeader() {
  const sections: Array<[string, string]> = [
    ['Lifecycle', '#lifecycle'],
    ['Capabilities', '#capabilities'],
    ['Guarantees', '#guarantees'],
  ];
  return (
    <header className="flex h-[72px] items-center justify-between border-b border-hairline">
      <div className="flex items-center gap-2.5">
        <AegisMark className="h-5 w-5" />
        <span className="text-body font-bold tracking-tight text-ink-primary">Aegis</span>
      </div>
      <nav aria-label="Sections" className="hidden items-center gap-7 sm:flex">
        {sections.map(([label, href]) => (
          <a
            key={href}
            href={href}
            className="text-meta font-semibold text-ink-tertiary transition-colors
                       duration-hover hover:text-ink-primary"
          >
            {label}
          </a>
        ))}
      </nav>
    </header>
  );
}

function Hero() {
  return (
    <section className="py-16 sm:py-24">
      <div
        className="mb-7 inline-flex items-center gap-2 rounded-full border border-hairline
                   bg-surface-1 px-3 py-1.5"
      >
        <span
          aria-hidden
          className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-status-success"
        />
        <span className="text-meta font-semibold tracking-wide text-ink-secondary">
          AI-native reliability operations
        </span>
      </div>

      <h1 className="text-[clamp(2.75rem,6.5vw,4.5rem)] font-extrabold leading-[0.95] tracking-[-0.04em] text-ink-primary">
        Production incidents,
        <br />
        <span className="bg-gradient-to-b from-ink-primary to-ink-tertiary bg-clip-text text-transparent">
          under control.
        </span>
      </h1>

      <p className="mt-7 max-w-[62ch] text-[0.95rem] font-medium leading-[1.75] text-ink-secondary">
        Aegis investigates failures, correlates evidence, localises root causes,
        tests remediations in isolation and verifies that they actually worked —
        then either acts within a deterministic policy or asks a human, with the
        full case already assembled.
      </p>

      <ol
        aria-label="Incident lifecycle"
        className="mt-10 flex flex-wrap items-center gap-x-2.5 gap-y-2"
      >
        {PIPELINE.map((step, index) => (
          <li key={step} className="flex items-center gap-2.5">
            <span className="text-meta font-bold uppercase tracking-[0.1em] text-ink-secondary">
              {step}
            </span>
            {index < PIPELINE.length - 1 && (
              <span aria-hidden className="text-ink-tertiary/50">
                →
              </span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

function Capabilities() {
  return (
    <section id="capabilities" className="scroll-mt-8 border-t border-hairline py-16">
      <SectionHeading
        eyebrow="A reliability control plane"
        title="AI that behaves like a system, not a chat window."
      />
      <div className="mt-9 grid gap-3 sm:grid-cols-2">
        {CAPABILITIES.map((capability, index) => (
          <article
            key={capability.title}
            className="rounded-card border border-hairline bg-surface-1 p-5
                       transition-colors duration-hover hover:border-line"
          >
            <div className="text-meta font-bold tabular-nums text-ink-tertiary">
              {String(index + 1).padStart(2, '0')}
            </div>
            <h3 className="mt-6 text-h3 font-bold tracking-tight text-ink-primary">
              {capability.title}
            </h3>
            <p className="mt-2 text-meta font-medium leading-[1.7] text-ink-secondary">
              {capability.body}
            </p>
          </article>
        ))}
      </div>
    </section>
  );
}

function Guarantees() {
  return (
    <section id="guarantees" className="scroll-mt-8 border-t border-hairline py-16">
      <SectionHeading
        eyebrow="Safety model"
        title="What Aegis will not do."
        body="These are structural properties of the codebase, not settings. Several are enforced by the type system rather than by review."
      />
      <ul className="mt-9">
        {GUARANTEES.map((guarantee) => (
          <li
            key={guarantee.claim}
            className="flex gap-4 border-b border-hairline py-4 last:border-b-0"
          >
            <span
              aria-hidden
              className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-status-success"
            />
            <div className="min-w-0">
              <p className="text-body font-semibold text-ink-primary">{guarantee.claim}</p>
              <p className="mt-1 text-meta font-medium leading-[1.7] text-ink-tertiary">
                {guarantee.why}
              </p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function LandingFooter() {
  return (
    <footer
      className="flex flex-col gap-2 border-t border-hairline py-8
                 sm:flex-row sm:items-center sm:justify-between"
    >
      <span className="text-meta font-medium text-ink-tertiary">
        Aegis — AI-native SRE control plane
      </span>
      <span className="text-meta font-semibold text-ink-secondary">Evidence before action.</span>
    </footer>
  );
}

function SectionHeading({
  eyebrow,
  title,
  body,
}: {
  eyebrow: string;
  title: string;
  body?: string;
}) {
  return (
    <div className="max-w-[56ch]">
      <div className="text-meta font-bold uppercase tracking-[0.14em] text-ink-tertiary">
        {eyebrow}
      </div>
      <h2 className="mt-3 text-[clamp(1.6rem,3.4vw,2.35rem)] font-extrabold leading-[1.08] tracking-[-0.035em] text-ink-primary">
        {title}
      </h2>
      {body && (
        <p className="mt-4 text-body font-medium leading-[1.75] text-ink-secondary">{body}</p>
      )}
    </div>
  );
}
