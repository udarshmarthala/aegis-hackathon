import { NextResponse } from 'next/server';

/**
 * Container healthcheck for the web service.
 *
 * Deliberately does NOT probe the API: this answers "is the Next.js server up".
 * Conflating it with backend health would make Docker restart a perfectly
 * healthy web container whenever the API was slow.
 */
export const dynamic = 'force-dynamic';

export function GET() {
  return NextResponse.json({ status: 'ok', service: 'aegis-web' });
}
