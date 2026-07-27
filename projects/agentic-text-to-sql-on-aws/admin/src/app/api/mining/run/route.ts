// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * POST /api/mining/run {hours} — MCP `mine_candidates` (Track B 후보 채굴).
 *
 * orchestrator 가 남기는 `t2sql_query_record` 로그를 읽어 fewshot/term 후보를 candidate 로
 * 적재한다. 채굴기는 admin-mcp 도구이므로(Lambda 아님 — 도구 평면 유지) 여기서도 사용자 JWT
 * OBO 로 Gateway 를 거친다 → Cedar 가 Manager/Admin 만 허용하고 DynamoDB 쓰기는 단일 지점.
 *
 * 채굴 결과는 승인 큐(candidate)에 쌓이므로, 실제 반영은 Manager 승인 후다(사람 승인 게이트).
 */

import { handle, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { normalizeHours } from '@/lib/eval';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';

export const dynamic = 'force-dynamic';

export async function POST(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const body = await readJson(request);

    const result = await callAdminTool(principal.accessToken, 'mine_candidates', {
      hours: normalizeHours(body.hours),
      actor: principal.username,
    });
    return mcpResponse(result, 202);
  });
}
