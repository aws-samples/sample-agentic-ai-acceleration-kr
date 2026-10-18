FROM node:20-slim AS builder
WORKDIR /app

COPY package.json yarn.lock ./
RUN corepack enable && yarn install --frozen-lockfile

COPY . .
# webpack, not Next 16's default Turbopack: Turbopack's Rust fetcher drops a
# share of the hundreds of next/font/google requests (IBM Plex Sans KR ships one
# file per unicode-range subset) when run inside a container, and the build then
# fails on "Can't resolve '@vercel/turbopack-next/internal/font/google/font'".
# Measured 2026-10-17 on OrbStack: Turbopack failed 6 of 6 builds (1-128 fonts
# each, arm64 and amd64, host networking too); webpack passed 2 of 2. Local
# `next dev` still uses Turbopack.
RUN yarn next build --webpack

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production

COPY --from=builder /app/package.json /app/yarn.lock ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.ts ./next.config.ts

EXPOSE 3000
CMD ["yarn", "start"]
