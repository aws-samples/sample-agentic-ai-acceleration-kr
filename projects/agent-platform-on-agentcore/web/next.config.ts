import type { NextConfig } from "next";

// Backend paths are proxied through the Next.js origin so a single forwarded port
// (3000) is enough — the browser never needs to reach 8000 directly. The proxy
// is the Route Handler in src/app/api/[[...path]]/route.ts (src/lib/backend-proxy.ts),
// which reads BACKEND_ORIGIN at request time; next.config rewrites would freeze
// the destination into the build.
const nextConfig: NextConfig = {
  // MCP Apps 샌드박스는 호스트와 다른 출처에서 서빙돼야 하므로 (규격 MUST), 로컬
  // 개발은 같은 앱을 두 포트에서 띄운다. 그런데 Next는 `.next/dev/lock` 으로 빌드
  // 디렉터리를 선점해서 두 번째 `next dev` 가 "Another next dev server is already
  // running" 으로 죽는다 — 락이 포트가 아니라 distDir 단위다.
  //
  // 그래서 샌드박스 쪽만 NEXT_DIST_DIR 로 다른 디렉터리를 쓰게 한다 (run.sh 가 넘긴다).
  distDir: process.env.NEXT_DIST_DIR || ".next",
  // Reaching the dev server through a forwarded port from another host makes the
  // browser's origin something other than localhost, and Next blocks /_next/*
  // dev resources for unknown origins — the app then hangs on "Loading..." and
  // the Threads panel reports "Failed to fetch". Set DEV_ORIGINS (comma-separated)
  // to whatever host you reach the dev server through; the addresses are
  // machine-specific, so none are hardcoded here.
  allowedDevOrigins: [
    "127.0.0.1",
    ...(process.env.DEV_ORIGINS?.split(",").map((o) => o.trim()).filter(Boolean) ?? []),
  ],
  experimental: {
    // The proxy Route Handler inherits this, and unset it defaults to 30s
    // applied as an *idle* timeout on the upstream socket
    // (`v.setTimeout(30000, () => v.abort())` in Next's bundled http-proxy). A
    // harness emits nothing while its tools run inside AWS, so a single tool
    // taking longer than that aborted the request and the chat lost the turn —
    // measured: a 25s gap survives, a 40s gap kills it with EPIPE upstream.
    //
    // The server also heartbeats every 10s now (HEARTBEAT_INTERVAL_SECONDS in
    // services/streaming_service.py), which is the real fix — this is the floor
    // under it, and it covers the non-streaming proxied routes too. Finite on
    // purpose: `null` disables the timeout entirely but the config schema is
    // `z.number().gte(0)` and rejects it, and `0` falls back to the 30s default
    // through `proxyTimeout || 30000`.
    proxyTimeout: 3_600_000,
  },
};

export default nextConfig;
