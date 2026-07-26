// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Gateway MCP 클라이언트 — semantic 쓰기 경로의 단일 통로 (§8.0 / §8.3).
 *
 * 설계 근거
 * --------
 * admin web 은 DynamoDB 를 **직접 쓰지 않는다**. 큐레이션·승인·데이터소스 작업은 모두
 * `사용자 JWT Bearer → Gateway MCP → datasource-admin-mcp` 경로로 흐른다. 그 결과
 *   (1) DynamoDB 단일 쓰기 지점 유지,
 *   (2) Cedar 가 Manager/Admin 인가를 도구 단위로 강제,
 *   (3) 사용자별 JWT On-Behalf-Of(M3 이월 부채)가 admin 경로에서 실현된다.
 *
 * 도구명 규약
 * ----------
 * Gateway 는 target 이름을 프리픽스로 붙인다: `<TargetName>___<toolName>` (트리플 언더스코어).
 * 프리픽스를 하드코딩하면 target 개명에 깨지므로, 최초 호출 시 `tools/list` 로 실제 도구명을
 * 받아 **suffix 매칭**으로 해석하고(방어적 구현) 실패 시 관례 프리픽스로 폴백한다.
 *
 * 커넥션 수명
 * ----------
 * 사용자 토큰마다 인가 컨텍스트가 다르므로 커넥션을 캐시하지 않고 요청 단위로 열고 닫는다
 * (Cedar 인가가 토큰에 묶여 있어 재사용은 권한 누출 위험).
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { ADMIN_MCP_TARGET, requiredEnv } from './env';

/** §8.3 datasource-admin-mcp 도구명 (프리픽스 없는 순수 이름). */
export type AdminToolName =
  | 'list_entities'
  | 'get_entity'
  | 'put_entity'
  | 'publish_entity'
  | 'unpublish_entity'
  | 'register_datasource'
  | 'test_datasource'
  | 'crawl_schema';

/** 도구 반환 규약: 성공 `{status:"ok",...}` / 실패 `{status:"error",message}`. */
export interface McpToolResult {
  status: 'ok' | 'error';
  message?: string;
  [key: string]: unknown;
}

/** Gateway 노출 도구명 목록에서 suffix 매칭으로 실제 이름을 찾는다. */
function resolveToolName(available: string[], tool: AdminToolName): string {
  // 1순위: 정확한 관례 프리픽스.
  const conventional = `${ADMIN_MCP_TARGET}___${tool}`;
  if (available.includes(conventional)) return conventional;
  // 2순위: `___<tool>` 로 끝나는 임의 target 프리픽스 (target 개명 대응).
  const bySuffix = available.find((name) => name.endsWith(`___${tool}`));
  if (bySuffix) return bySuffix;
  // 3순위: 프리픽스 없이 노출된 경우 (direct MCP 연결 등).
  if (available.includes(tool)) return tool;
  // 폴백: 관례 프리픽스로 시도 (tools/list 가 Cedar 로 가려진 경우 오류 메시지가 더 명확).
  return conventional;
}

/** MCP CallToolResult 의 content 배열에서 JSON dict 를 뽑아낸다. */
function parseToolResult(raw: unknown): McpToolResult {
  const result = raw as { content?: Array<{ type?: string; text?: string }>; isError?: boolean };
  const texts = (result?.content ?? [])
    .filter((c) => c?.type === 'text' && typeof c.text === 'string')
    .map((c) => c.text as string);
  for (const text of texts) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') return parsed as McpToolResult;
    } catch {
      // JSON 이 아니면 다음 content 블록을 시도.
    }
  }
  const fallback = texts.join('\n').trim();
  if (result?.isError) {
    return { status: 'error', message: fallback || 'MCP 도구가 오류를 반환했습니다' };
  }
  return { status: 'ok', raw: fallback };
}

/**
 * 사용자 AccessToken(Bearer)으로 Gateway MCP 도구를 1회 호출한다.
 *
 * @param accessToken 사용자 Cognito AccessToken (OBO — 서비스 계정 토큰 아님)
 * @param tool §8.3 도구명
 * @param args 도구 인자 (actor 는 호출자가 JWT username 으로 채워 넘긴다)
 */
export async function callAdminTool(
  accessToken: string,
  tool: AdminToolName,
  args: Record<string, unknown> = {}
): Promise<McpToolResult> {
  const gatewayUrl = requiredEnv('GATEWAY_URL');
  const transport = new StreamableHTTPClientTransport(new URL(gatewayUrl), {
    requestInit: {
      headers: {
        // 사용자 토큰을 그대로 전달 — Gateway 가 JWT 를 검증하고 Cedar 가 인가한다.
        Authorization: `Bearer ${accessToken}`,
      },
    },
  });
  const client = new Client({ name: 'agentic-t2sql-admin-web', version: '0.1.0' });

  try {
    await client.connect(transport);

    // tools/list 로 실제 노출 도구명을 확인 (Cedar 가 미인가 도구를 목록에서 제외한다).
    let available: string[] = [];
    try {
      const listed = await client.listTools();
      available = (listed.tools ?? []).map((t) => t.name);
    } catch (error) {
      console.warn('[admin-mcp] tools/list 실패 — 관례 프리픽스로 폴백:', error);
    }

    const resolved = resolveToolName(available, tool);
    const raw = await client.callTool({ name: resolved, arguments: args });
    return parseToolResult(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { status: 'error', message: `Gateway MCP 호출 실패: ${message}` };
  } finally {
    // 커넥션은 요청 단위로 정리 (토큰별 인가 컨텍스트 분리).
    await client.close().catch(() => undefined);
  }
}

/** MCP 결과를 HTTP 응답으로 변환 — 도구 오류는 502(업스트림 실패)로 표면화. */
export function mcpResponse(result: McpToolResult, okStatus = 200): Response {
  if (result.status === 'error') {
    return Response.json(result, { status: 502 });
  }
  return Response.json(result, { status: okStatus });
}
