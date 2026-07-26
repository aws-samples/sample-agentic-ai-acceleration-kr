// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/semantic/entities/{type}/{id} — MCP `get_entity` (상세 조회, 편집 폼 프리필용)
 * PUT  /api/semantic/entities/{type}/{id} — MCP `put_entity`(status=candidate 기본, §8.4)
 *
 * 쓰기는 항상 candidate 로 들어가고, 발행은 별도 publish 경로를 거친다(승인 워크플로 분리).
 * `actor` 인자에는 JWT username 을 실어 감사 기록을 남긴다.
 */

import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

interface RouteContext {
  params: { type: string; id: string };
}

export async function GET(request: Request, { params }: RouteContext) {
  return handle(async () => {
    const principal = await requireManager(request);
    const result = await callAdminTool(principal.accessToken, 'get_entity', {
      entity_type: decodeURIComponent(params.type),
      entity_id: decodeURIComponent(params.id),
    });
    return mcpResponse(result);
  });
}

export async function PUT(request: Request, { params }: RouteContext) {
  return handle(async () => {
    const principal = await requireManager(request);
    const body = await readJson(request);

    const payload = body.payload;
    if (payload == null || typeof payload !== 'object' || Array.isArray(payload)) {
      return jsonError('payload(객체)가 필요합니다', 400);
    }
    // status 는 candidate|published 만 허용하며, 미지정 시 candidate.
    const status = typeof body.status === 'string' ? body.status : 'candidate';
    if (status !== 'candidate' && status !== 'published') {
      return jsonError('status 는 candidate 또는 published 여야 합니다', 400);
    }

    const result = await callAdminTool(principal.accessToken, 'put_entity', {
      entity_type: decodeURIComponent(params.type),
      entity_id: decodeURIComponent(params.id),
      payload,
      status,
      actor: principal.username,
    });
    return mcpResponse(result);
  });
}
