// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/semantic/entities?type=&status= — MCP `list_entities` (사용자 토큰 OBO).
 */

import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const url = new URL(request.url);
    const entityType = url.searchParams.get('type');
    const status = url.searchParams.get('status');

    const args: Record<string, unknown> = {};
    if (entityType) args.entity_type = entityType;
    if (status) args.status = status;

    const result = await callAdminTool(principal.accessToken, 'list_entities', args);
    return mcpResponse(result);
  });
}
