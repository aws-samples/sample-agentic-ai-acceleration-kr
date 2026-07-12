// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getGatewayToken } from './auth';
import { GATEWAY_URL } from './constants';

/**
 * Minimal server-side MCP (Model Context Protocol) client for the AgentCore
 * Gateway.
 *
 * The gateway speaks JSON-RPC 2.0 over a single POST endpoint (its `/mcp` URL).
 * It does NOT support REST-style sub-paths like `/mcp/tools/list` — those return
 * "Http operation is not supported for gateway protocol type MCP". It also
 * requires the negotiated protocol version header.
 */

// Protocol version advertised by the gateway (see get-gateway -> protocolConfiguration.mcp.supportedVersions).
const MCP_PROTOCOL_VERSION = '2025-11-25';

export interface McpTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

export interface McpToolCallResult {
  isError: boolean;
  content: Array<{ type: string; text?: string; [k: string]: unknown }>;
}

interface JsonRpcResponse<T> {
  jsonrpc: '2.0';
  id: number | string;
  result?: T;
  error?: { code: number; message: string; data?: unknown };
}

let rpcId = 0;

/**
 * Parses a gateway response that may be either application/json or an
 * SSE-framed (text/event-stream) single JSON-RPC message.
 */
async function parseRpcBody<T>(res: Response): Promise<JsonRpcResponse<T>> {
  const contentType = res.headers.get('content-type') || '';
  const raw = await res.text();

  if (contentType.includes('text/event-stream')) {
    // Extract the JSON payload from the last `data:` line.
    const dataLines = raw
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .filter(Boolean);
    const payload = dataLines[dataLines.length - 1];
    if (!payload) {
      throw new Error(`Empty SSE response from gateway: ${raw.slice(0, 200)}`);
    }
    return JSON.parse(payload) as JsonRpcResponse<T>;
  }

  return JSON.parse(raw) as JsonRpcResponse<T>;
}

async function rpc<T>(method: string, params: Record<string, unknown>): Promise<T> {
  if (!GATEWAY_URL) {
    throw new Error('NEXT_PUBLIC_TOOL_GATEWAY_URL is not configured');
  }

  const token = await getGatewayToken();
  const id = ++rpcId;

  const res = await fetch(GATEWAY_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      'MCP-Protocol-Version': MCP_PROTOCOL_VERSION,
    },
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
    cache: 'no-store',
  });

  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Gateway HTTP ${res.status}: ${detail.slice(0, 300)}`);
  }

  const body = await parseRpcBody<T>(res);

  if (body.error) {
    throw new Error(`MCP error ${body.error.code}: ${body.error.message}`);
  }

  if (body.result === undefined) {
    throw new Error('MCP response missing result');
  }

  return body.result;
}

// In SEMANTIC search mode the gateway replaces page 1 of tools/list with a
// single meta search-tool and paginates the real tools behind nextCursor.
export const SEARCH_META_TOOL = 'x_amz_bedrock_agentcore_search';

/**
 * Parses a tools/list result and filters out the synthetic search meta-tool.
 */
export function parseToolsResult(result: { tools?: McpTool[] } | undefined): McpTool[] {
  const tools = result?.tools ?? [];
  return tools.filter((t) => t.name !== SEARCH_META_TOOL);
}

/**
 * Lists the tools exposed by the gateway, following pagination so the full set
 * is returned even when the gateway is in SEMANTIC search mode. The synthetic
 * search meta-tool is dropped — callers want the actual MCP tools.
 */
export async function listTools(): Promise<McpTool[]> {
  const tools: McpTool[] = [];
  let cursor: string | undefined;

  do {
    const params = cursor ? { cursor } : {};
    const result = await rpc<{ tools?: McpTool[]; nextCursor?: string }>('tools/list', params);
    const parsed = parseToolsResult(result);
    tools.push(...parsed);
    cursor = result.nextCursor;
  } while (cursor);

  return tools;
}

/** Calls a single tool by its (namespaced) name. */
export async function callTool(
  name: string,
  args: Record<string, unknown>
): Promise<McpToolCallResult> {
  return rpc<McpToolCallResult>('tools/call', { name, arguments: args });
}

/**
 * Unwraps a tool-call result into the structured search payload our Lambda
 * tools return. Each tool returns JSON-as-text inside content[0].text.
 */
export function unwrapToolText(result: McpToolCallResult): unknown {
  const textPart = result.content?.find((c) => c.type === 'text' && typeof c.text === 'string');
  if (!textPart?.text) {
    return { raw: result };
  }
  try {
    return JSON.parse(textPart.text);
  } catch {
    return { text: textPart.text };
  }
}
