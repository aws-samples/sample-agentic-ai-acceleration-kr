// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** POST /api/semantic/entities/{type}/{id}/publish — MCP `publish_entity` (승인). */

import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { type: string; id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const result = await callAdminTool(principal.accessToken, 'publish_entity', {
      entity_type: decodeURIComponent(params.type),
      entity_id: decodeURIComponent(params.id),
      actor: principal.username,
    });
    return mcpResponse(result);
  });
}
