// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET  /api/datasources — MCP `list_entities(entity_type="datasource")`
 * POST /api/datasources — MCP `register_datasource` (§8.4)
 *
 * 등록 시 `config`(호스트·DB·자격증명 등)는 admin web 이 저장하지 않는다. MCP 서버가
 * Secrets Manager `agentic-t2sql/datasource/<id>` 에 넣고, 자격증명을 제외한 메타만
 * DynamoDB 에 candidate 로 기록한다 — 시크릿이 admin web 상태에 남지 않는 구조.
 */

import { handle, jsonError, readJson } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { callAdminTool, mcpResponse } from '@/lib/mcp-client';
import { DATASOURCE_ENGINES } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const result = await callAdminTool(principal.accessToken, 'list_entities', {
      entity_type: 'datasource',
    });
    return mcpResponse(result);
  });
}

export async function POST(request: Request) {
  return handle(async () => {
    const principal = await requireManager(request);
    const body = await readJson(request);

    const datasourceId = typeof body.datasource_id === 'string' ? body.datasource_id.trim() : '';
    const engine = typeof body.engine === 'string' ? body.engine : '';
    const config = body.config;

    if (!datasourceId) return jsonError('datasource_id 가 필요합니다', 400);
    if (!(DATASOURCE_ENGINES as readonly string[]).includes(engine)) {
      return jsonError(`engine 은 ${DATASOURCE_ENGINES.join(' | ')} 중 하나여야 합니다`, 400);
    }
    if (config == null || typeof config !== 'object' || Array.isArray(config)) {
      return jsonError('config(객체)가 필요합니다', 400);
    }

    const result = await callAdminTool(principal.accessToken, 'register_datasource', {
      datasource_id: datasourceId,
      engine,
      config,
      actor: principal.username,
    });
    return mcpResponse(result, 201);
  });
}
