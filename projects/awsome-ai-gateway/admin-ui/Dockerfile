# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# Stage 1: deps
FROM public.ecr.aws/docker/library/node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# Stage 2: builder
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# Tool Gateway 대시보드 게이트: NEXT_PUBLIC_* 는 next build 시점에 브라우저 번들로
# 인라인되므로 build arg 로 받는다. 비어 있으면 대시보드는 비활성(기존 동작).
ARG NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=false
ARG NEXT_PUBLIC_TOOL_GATEWAY_URL=
ARG NEXT_PUBLIC_TOOL_GATEWAY_ID=
ARG NEXT_PUBLIC_TOOL_GATEWAY_REGION=us-east-1
ENV NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=$NEXT_PUBLIC_TOOL_GATEWAY_ENABLED \
    NEXT_PUBLIC_TOOL_GATEWAY_URL=$NEXT_PUBLIC_TOOL_GATEWAY_URL \
    NEXT_PUBLIC_TOOL_GATEWAY_ID=$NEXT_PUBLIC_TOOL_GATEWAY_ID \
    NEXT_PUBLIC_TOOL_GATEWAY_REGION=$NEXT_PUBLIC_TOOL_GATEWAY_REGION
RUN npm run build

# Stage 3: runner
FROM public.ecr.aws/docker/library/node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# non-root user (SECURITY-06)
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Healthcheck (wget 사용 — alpine에는 curl 없음)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD wget -q --tries=1 --spider http://localhost:3000/api/health || exit 1

CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]
