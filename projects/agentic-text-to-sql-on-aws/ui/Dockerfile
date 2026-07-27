# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ARM64 멀티 스테이지 빌드 (ECS Fargate ARM64 / Graviton). Next standalone output.
# 로컬 빌드는 docker 기본, 실패 시 finch(`finch build --platform linux/arm64 ...`) 폴백.

# Stage 1: deps
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# Stage 2: builder
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# Stage 3: runner
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# non-root 사용자 (최소 권한)
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Healthcheck (wget 사용 — alpine 에는 curl 없음)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD wget -q --tries=1 --spider http://localhost:3000/api/health || exit 1

CMD ["node", "server.js"]
