// The deployed API origin. A production Vercel build that was never given
// NEXT_PUBLIC_API_BASE_URL - or was given a localhost value copied from a dev
// .env - would otherwise ship a bundle that calls the visitor's own machine,
// and every sign-in would fail at the first API call. An explicit, non-local
// value in the Vercel project always wins.
const DEPLOYED_API_BASE_URL = 'https://d2schpsj5u9197.cloudfront.net';
const configuredApi = process.env.NEXT_PUBLIC_API_BASE_URL;
const apiBaseUrl =
  process.env.VERCEL_ENV === 'production' &&
  (!configuredApi || /localhost|127\.0\.0\.1/.test(configuredApi))
    ? DEPLOYED_API_BASE_URL
    : configuredApi;

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: apiBaseUrl ? { NEXT_PUBLIC_API_BASE_URL: apiBaseUrl } : {},
  // standalone emits a minimal server bundle, which keeps the runtime image
  // small and free of the full node_modules tree. It is a self-hosting mode:
  // a managed platform builds its own serverless output and never reads
  // .next/standalone, so emitting it there is pure build cost. The Dockerfile
  // opts in explicitly; every other build leaves it off.
  output: process.env.BUILD_STANDALONE === '1' ? 'standalone' : undefined,
  poweredByHeader: false,
  // ESLint runs during the build. It was previously skipped, which meant a
  // green build said nothing about lint - and since the repository had no
  // ESLint config at all, nothing was checking these files anywhere.
  eslint: { ignoreDuringBuilds: false },
  // The same reasoning for types: a build that ignores type errors is a build
  // that ships them.
  typescript: { ignoreBuildErrors: false },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
    ];
  },
};
export default nextConfig;
