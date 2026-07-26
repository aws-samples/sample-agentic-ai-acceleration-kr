// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** POST /api/datasources/{id}/test — MCP `test_datasource` (연결 테스트, §8.4). */

import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const result = await callAdminTool(principal.accessToken, 'test_datasource', {
      datasource_id: decodeURIComponent(params.id),
    });
    return mcpResponse(result);
  });
}
