/**
 * MCP Apps 릴레이 API 클라이언트.
 *
 * 서버가 엔드포인트를 레지스트리에서 해석하므로 여기서 MCP 서버 설정을 보내지 않는다.
 */
import { authedFetch } from "@/lib/http";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "";

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await authedFetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export async function readUiResource(
  recordId: string,
  uri: string,
): Promise<{ text: string; uiMeta: Record<string, unknown> }> {
  const body = await post<{ text: string; ui_meta: Record<string, unknown> }>(
    "/api/mcp-apps/resources/read",
    { record_id: recordId, uri },
  );
  return { text: body.text, uiMeta: body.ui_meta ?? {} };
}

export async function callAppTool(
  recordId: string,
  toolName: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  const body = await post<{ result: unknown }>("/api/mcp-apps/tools/call", {
    record_id: recordId,
    tool_name: toolName,
    arguments: args,
  });
  return body.result;
}

/**
 * 앱을 띄운 툴의 정의(MCP Tool). 규격 `hostContext.toolInfo.tool` 에 넣어 View 가 자기를
 * 부른 툴의 이름·스키마를 알 수 있게 한다. 실패해도 앱은 뜬다 — 호출자가 무시한다.
 */
export async function describeAppTool(
  recordId: string,
  toolName: string,
): Promise<Record<string, unknown>> {
  const body = await post<{ tool: Record<string, unknown> }>("/api/mcp-apps/tools/describe", {
    record_id: recordId,
    tool_name: toolName,
  });
  return body.tool;
}
