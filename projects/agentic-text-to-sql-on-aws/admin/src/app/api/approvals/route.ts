// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/approvals — 승인 큐 (§8.4).
 *
 * `list_entities(status="candidate")` 와 동일하지만, 승인 워크플로의 의미를 URL 로 드러내고
 * `type` 쿼리로 좁힐 수 있게 둔다. 승인 실행은 `.../publish` 경로를 사용한다.
 */

import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const entityType = new URL(request.url).searchParams.get('type');

    const args: Record<string, unknown> = { status: 'candidate' };
    if (entityType) args.entity_type = entityType;

    const result = await callAdminTool(principal.accessToken, 'list_entities', args);
    return mcpResponse(result);
  });
}
