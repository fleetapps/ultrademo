# The player (apps/web), from Next.js's official with-docker example (standalone output):
#   docker build -f infra/docker/web.Dockerfile apps/web
# Runtime configuration is read per request (the pages are dynamic), so one image serves every
# environment: ULTRADEMO_API_URL (server side only) and ULTRADEMO_LIVEKIT_URL (for the CSP).
ARG NODE_VERSION=22-bookworm-slim

FROM node:${NODE_VERSION} AS dependencies
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    corepack enable pnpm && pnpm install --frozen-lockfile

FROM node:${NODE_VERSION} AS builder
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN corepack enable pnpm && pnpm build

FROM node:${NODE_VERSION} AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN mkdir .next && chown node:node .next
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
