# Aegis console

The operator console for Aegis 2.0 — Next.js 15 (App Router), React 19, TypeScript,
Tailwind, TanStack Query.

It is a **pure client of the Aegis API**. It holds no incident state of its own,
renders nothing at build time that depends on the backend, and deploys happily
before the API exists — every surface will simply report that its source is
unreachable, which is a state this console draws deliberately rather than
disguising as an absence of data.

## Local development

```bash
npm ci
cp .env.example .env.local   # then edit
npm run dev
```

The full stack (API, Postgres, Neo4j, Redis, telemetry) runs from the repository
root with `make up`. See [docs/local-development.md](../docs/local-development.md).

## Checks

```bash
npm run typecheck   # tsc --noEmit
npm run lint        # eslint .
npm run test        # vitest run
npm run build       # next build — enforces lint and types
```

`next.config.mjs` sets `eslint.ignoreDuringBuilds` and `typescript.ignoreBuildErrors`
to `false`, so a green build genuinely means lint and types passed. The build does
not execute tests; run them too.

## Environment

Every variable is documented in [`.env.example`](.env.example). Two properties are
easy to get wrong:

- **`NEXT_PUBLIC_*` values are inlined at build time**, not read at runtime.
  Changing one in a hosting dashboard does nothing until you redeploy.
- **`NEXT_PUBLIC_AEGIS_ENV` must not be `local`** on anything reachable from the
  internet. Only that exact value enables the development sign-in bypass. Leaving
  the variable unset withholds the bypass, which is the safe default.

| Variable | Required | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | yes | Origin of the Aegis API as the **browser** reaches it. Must be `https://` when the console is served over https, or the request is blocked as mixed content. |
| `NEXT_PUBLIC_AEGIS_ENV` | yes | `local` \| `staging` \| `production`. |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | for Firebase sign-in | All three Firebase values are required together. |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | for Firebase sign-in | |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | for Firebase sign-in | |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | no | |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | no | |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | no | |
| `AEGIS_API_INTERNAL_URL` | no | Server-side only. A private address for the API (compose, ECS). Falls back to `NEXT_PUBLIC_API_BASE_URL`. |
| `BUILD_STANDALONE` | no | `1` only for the container image build. |

## Deploying to Vercel

1. **Import the repository** at [vercel.com/new](https://vercel.com/new) and set
   **Root Directory** to `frontend`. This is a monorepo with no root
   `package.json`, so detection at the repository root finds nothing. Leave
   "Include files outside the root directory" off. The framework preset, build
   command and install command are already pinned by `vercel.json`.

2. **Add the environment variables** from the table above to *Production* and
   *Preview*. At minimum set `NEXT_PUBLIC_API_BASE_URL` to the public `https://`
   origin of your API, and `NEXT_PUBLIC_AEGIS_ENV` to `production`.

3. **Deploy.** Vercel runs `npm ci` then `npm run build`. Lint and type errors
   fail the deploy by design.

4. **Allow the Vercel origin on the backend.** Add your `https://<project>.vercel.app`
   domain (and any custom domain) to `CORS_ALLOWED_ORIGINS` in the API environment
   and restart it. Production refuses wildcards. Until this is done every request
   from the console fails CORS preflight.

5. **If you use Firebase sign-in**, add the Vercel domain to Firebase Console →
   Authentication → Settings → Authorized domains, or the sign-in popup fails with
   `auth/unauthorized-domain`.

### Notes

- `next.config.mjs` emits Next.js **standalone** output only when
  `BUILD_STANDALONE=1`. That mode exists for the container image; Vercel builds its
  own serverless output and never reads `.next/standalone`, so leaving it on there
  is wasted build time.
- Changing a `NEXT_PUBLIC_*` value in the Vercel dashboard requires a **redeploy**
  to take effect.
- The console streams incident updates over SSE. Vercel proxies streaming
  responses, but the stream goes directly to your API origin, so that origin must
  be publicly reachable over https.

## Deploying as a container

`Dockerfile` builds the standalone image used by Docker Compose and ECS. It passes
`BUILD_STANDALONE=1` and every `NEXT_PUBLIC_*` value as build arguments, because
those must be present at build time rather than only at runtime.

## Structure

```
src/app/(marketing)     public landing page and sign-in
src/app/(console)       the operator console, one route group behind auth
src/app/api/healthz     liveness endpoint
src/components/ui       states.tsx — the empty / unavailable / error primitives
src/lib/api.ts          the single fetch path; ApiError vs NetworkError
src/lib/console-api.ts  typed endpoints built on top of it
```

The rule that shapes most of this code: **"found nothing" and "could not look" are
different facts and must never render the same way.** `states.tsx` holds the
primitives that keep them apart, and `QueryFailure` picks between them from the
error class. A component that renders an empty list when its query failed is a
defect, not a style choice.
