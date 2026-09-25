/**
 * The public shell.
 *
 * No sidebar, no command palette, no query client: an unauthenticated visitor
 * should never download the operator console. Keeping the landing page in its
 * own route group is what makes that a structural guarantee rather than a
 * bundling accident.
 */
export default function MarketingLayout({ children }: { children: React.ReactNode }) {
  return <div id="main">{children}</div>;
}
