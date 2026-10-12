import { type NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/backend-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path?: string[] }> };

async function handler(request: NextRequest, { params }: Ctx): Promise<Response> {
  const { path } = await params;
  return proxyToBackend(request, ["api", ...(path ?? [])]);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
export const OPTIONS = handler;
