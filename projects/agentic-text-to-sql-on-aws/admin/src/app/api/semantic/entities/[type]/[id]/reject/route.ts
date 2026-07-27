// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/semantic/entities/{type}/{id}/reject — MCP `reject_entity` (반려).
 *
 * status=rejected 로 전이하고 payload 에 `rejection_reason` 을 남긴다. rejected 는 승인 큐
 * (candidate) 에서 사라지고 파생 저장소(OpenSearch·Neptune)에도 노출되지 않는다 —
 * 반려 이력은 rejected 목록에서 확인하고, 재검토가 필요하면 발행(publish)으로 되살린다.
 */

import { handle, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function POST(request: Request, { params }: { params: { type: string; id: string } }) {
  return handle(async () => {
    const principal = await requireManager(request);
    const body = await readJson(request);
    const reason = typeof body.reason === 'string' ? body.reason.trim() : '';

    const result = await callAdminTool(principal.accessToken, 'reject_entity', {
      entity_type: decodeURIComponent(params.type),
      entity_id: decodeURIComponent(params.id),
      reason,
      actor: principal.username,
    });
    return mcpResponse(result);
  });
}
