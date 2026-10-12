import { type NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/backend-proxy";

// nodejs: BACKEND_ORIGIN is read at runtime (edge inlines env at build).
// force-dynamic: never cache — every call proxies live, and stream responses
// must not be statically optimized.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Optional catch-all so both `/threads` (list) and `/threads/<id>/runs/stream`
// (SSE) resolve here.
type Ctx = { params: Promise<{ path?: string[] }> };

async function handler(request: NextRequest, { params }: Ctx): Promise<Response> {
  const { path } = await params;
  return proxyToBackend(request, ["threads", ...(path ?? [])]);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
export const OPTIONS = handler;
