/**
 * Direct API client for FastAPI server
 * Replaces LangGraph SDK Client
 */

import { authedFetch } from "@/lib/http";

export interface ApiClientOptions {
  apiUrl?: string;
}

export class ApiClient {
  private apiUrl: string;

  constructor(options: ApiClientOptions = {}) {
    // Default to same-origin ("") so requests go through the Next.js rewrite
    // proxy; only use an absolute URL when explicitly configured.
    this.apiUrl =
      options.apiUrl ?? process.env.NEXT_PUBLIC_API_URL ?? "";
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const response = await authedFetch(`${this.apiUrl}${endpoint}`, options);

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      const detail = error.detail || response.statusText;
      // 403 is a role failure, not a stale session — refreshing would not help.
      if (response.status === 403) {
        throw new Error(detail || "권한이 없습니다.");
      }
      throw new Error(`HTTP ${response.status}: ${detail}`);
    }

    return response.json();
  }

  async getThread(threadId: string) {
    return this.request(`/threads/${threadId}`);
  }

  async searchThreads(params: {
    limit?: number;
    offset?: number;
    sort_by?: string;
    sort_order?: "asc" | "desc";
    status?: string;
    metadata?: Record<string, any>;
  }) {
    const queryParams = new URLSearchParams();
    if (params.limit) queryParams.set("limit", params.limit.toString());
    if (params.offset) queryParams.set("offset", params.offset.toString());
    if (params.sort_by) queryParams.set("sort_by", params.sort_by);
    if (params.sort_order) queryParams.set("sort_order", params.sort_order);
    if (params.status) queryParams.set("status", params.status);
    if (params.metadata) queryParams.set("metadata", JSON.stringify(params.metadata));

    return this.request(`/threads?${queryParams.toString()}`);
  }

  async createThread(threadData?: Record<string, any>) {
    return this.request(`/threads`, {
      method: "POST",
      body: JSON.stringify(threadData || {}),
    });
  }

  async updateThreadState(
    threadId: string,
    update: { values?: Record<string, any>; metadata?: Record<string, any> }
  ) {
    return this.request(`/threads/${threadId}/state`, {
      method: "PATCH",
      body: JSON.stringify(update),
    });
  }

  async getThreadState(threadId: string) {
    return this.request(`/threads/${threadId}/state`);
  }

  async deleteThread(threadId: string) {
    return this.request(`/threads/${threadId}`, {
      method: "DELETE",
    });
  }

  async streamThread(
    threadId: string,
    request: {
      values?: Record<string, any>;
      config?: Record<string, any>;
      checkpoint?: any;
      command?: any;
      interrupt_before?: string[];
      interrupt_after?: string[];
    }
  ): Promise<ReadableStream<Uint8Array>> {
    const response = await authedFetch(
      `${this.apiUrl}/threads/${threadId}/runs/stream`,
      { method: "POST", body: JSON.stringify(request) }
    );

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(`HTTP ${response.status}: ${error.detail || response.statusText}`);
    }

    if (!response.body) {
      throw new Error("No response body");
    }

    return response.body;
  }

  async listMcpTools(mcpServers: Record<string, any>) {
    return this.request<{
      total_servers: number;
      total_tools: number;
      servers: Array<{
        server_name: string;
        url: string;
        success: boolean;
        tools: Array<{
          name: string;
          description?: string;
          input_schema?: Record<string, any>;
        }>;
        error?: string;
      }>;
    }>("/api/mcp/tools", {
      method: "POST",
      body: JSON.stringify({ mcp_servers: mcpServers }),
    });
  }

  async callMcpTool(
    serverName: string,
    serverConfig: Record<string, any>,
    toolName: string,
    args: Record<string, any> = {}
  ) {
    return this.request<{
      success: boolean;
      result?: any;
      error?: string;
    }>("/api/mcp/tools/call", {
      method: "POST",
      body: JSON.stringify({
        server_name: serverName,
        server_config: serverConfig,
        tool_name: toolName,
        arguments: args,
      }),
    });
  }

  /**
   * Live tools/list against a registered MCP record.
   *
   * Takes only the record id: the endpoint is resolved from the record's
   * descriptor server-side, and the server signs AgentCore endpoints with SigV4
   * — which is why a gateway record can be inspected here but not through
   * `listMcpTools`, where the browser supplies an unsigned URL.
   */
  async listRecordMcpTools(recordId: string) {
    return this.request<{
      record_id: string;
      endpoint: string;
      tools: Array<{
        name: string;
        description?: string;
        input_schema?: Record<string, any>;
        meta?: Record<string, any>;
      }>;
    }>(`/api/mcp/records/${encodeURIComponent(recordId)}/tools`);
  }

  /** Call one tool on a registered MCP record. No server config is sent. */
  async callRecordMcpTool(
    recordId: string,
    toolName: string,
    args: Record<string, any> = {}
  ) {
    return this.request<{
      success: boolean;
      result?: any;
      error?: string;
    }>("/api/mcp/records/tools/call", {
      method: "POST",
      body: JSON.stringify({
        record_id: recordId,
        tool_name: toolName,
        arguments: args,
      }),
    });
  }

  /** App user login via Cognito USER_PASSWORD_AUTH. */
  async login(params: { username: string; password: string }) {
    return this.request<{
      success: boolean;
      access_token?: string;
      id_token?: string;
      refresh_token?: string;
      expires_in?: number;
      error?: string;
    }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(params),
    });
  }

  /** Refresh the access/id token using a refresh token. */
  async refreshToken(refreshToken: string) {
    return this.request<{
      success: boolean;
      access_token?: string;
      id_token?: string;
      expires_in?: number;
      error?: string;
    }>("/api/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
  }

  async cognitoLogin(params: {
    client_id: string;
    username?: string;
    password?: string;
    client_secret?: string;
    user_pool_domain?: string;
    region?: string;
    auth_flow?: string;
  }) {
    return this.request<{
      success: boolean;
      access_token?: string;
      error?: string;
    }>("/api/mcp/cognito/login", {
      method: "POST",
      body: JSON.stringify({
        client_id: params.client_id,
        username: params.username,
        password: params.password,
        client_secret: params.client_secret,
        user_pool_domain: params.user_pool_domain,
        // Omit the region when the caller has none so the server falls back to
        // its configured COGNITO_REGION. Sending "us-east-1" from here overrode
        // that config, so a pool in another region could not be reached at all.
        region: params.region || undefined,
        auth_flow: params.auth_flow || "USER_PASSWORD_AUTH",
      }),
    });
  }
}

