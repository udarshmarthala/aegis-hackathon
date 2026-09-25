/**
 * The Aegis mark: a geometric shield with a negative-space A.
 *
 * Deliberately not clip-art, not a sparkle, not a robot. It must stay legible
 * at 16px (favicon) and in monochrome print, so it is built from three straight
 * strokes with no fine detail (UX spec section 5).
 */
export function AegisMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      role="img"
      aria-label="Aegis"
    >
      {/* shield silhouette */}
      <path
        d="M12 1.5 21 5v7.2c0 5-3.8 8.9-9 10.3C6.8 21.1 3 17.2 3 12.2V5l9-3.5Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      {/* negative-space A */}
      <path
        d="M12 7.6 8.4 16.4M12 7.6l3.6 8.8M9.9 13.4h4.2"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
