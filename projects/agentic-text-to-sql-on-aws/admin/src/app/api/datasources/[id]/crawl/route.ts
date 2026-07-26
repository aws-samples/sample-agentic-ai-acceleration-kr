// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/datasources/{id}/crawl — MCP `crawl_schema` (§8.4).
 *
 * information_schema 를 크롤해 table/column/join 엔티티를 **candidate** 로 적재한다.
 * 발행은 승인 큐를 거치므로, 크롤 직후에는 검색 평면에 반영되지 않는다.
 */

import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const result = await callAdminTool(principal.accessToken, 'crawl_schema', {
      datasource_id: decodeURIComponent(params.id),
      actor: principal.username,
    });
    return mcpResponse(result);
  });
}
