# syntax=docker/dockerfile:1.7
# Aegis web. Uses Next.js standalone output so the runtime layer ships only the
# server bundle and its traced dependencies, not the whole node_modules tree.

# ---------- deps -------------------------------------------------------------
FROM node:26-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# ---------- builder ----------------------------------------------------------
FROM node:26-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* values are inlined at build time, so they must be present here
# rather than only at runtime. These identify the project; they do not authorize.
# next.config.mjs emits standalone output only when asked. It is a self-hosting
# mode - a managed platform builds its own serverless output and never reads
# .next/standalone - so the image build is the one place that opts in.
ARG BUILD_STANDALONE=1
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_AEGIS_ENV
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET
ARG NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID
ARG NEXT_PUBLIC_FIREBASE_APP_ID
ENV BUILD_STANDALONE=$BUILD_STANDALONE \
    NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_AEGIS_ENV=$NEXT_PUBLIC_AEGIS_ENV \
    NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY \
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN \
    NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID \
    NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=$NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET \
    NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=$NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID \
    NEXT_PUBLIC_FIREBASE_APP_ID=$NEXT_PUBLIC_FIREBASE_APP_ID \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ---------- runtime ----------------------------------------------------------
FROM node:26-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# node:alpine already provides an unprivileged `node` user (uid 1000).
# The runtime ships no package manager. Next.js standalone output bundles
# everything server.js needs, and npm itself is never invoked here - but the
# node base image carries npm's own bundled dependencies, and those carry CVEs
# that fail the image scan even though no application code can reach them.
# Removing the installer removes the finding and shrinks the attack surface,
# rather than suppressing a genuine HIGH.
RUN rm -rf /usr/local/lib/node_modules/npm            /usr/local/bin/npm /usr/local/bin/npx            /usr/local/lib/node_modules/corepack            /usr/local/bin/corepack     && node --version

COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000

HEALTHCHECK --interval=20s --timeout=4s --start-period=20s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/api/healthz || exit 1

CMD ["node", "server.js"]
