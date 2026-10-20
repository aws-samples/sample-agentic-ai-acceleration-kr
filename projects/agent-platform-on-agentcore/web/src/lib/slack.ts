/**
 * Client for the Slack DM integration's admin API (server/routes/slack.py).
 *
 * Tokens are sent once — to `test` or `create` — and never come back: no
 * response carries them, and nothing here stores them.
 */

import { authedFetch } from "@/lib/http";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export interface SlackCheck {
  id: string;
  ok: boolean;
  message: string;
}

export interface SlackTestResult {
  ok: boolean;
  checks: SlackCheck[];
  workspace: {
    team_id?: string | null;
    team_name?: string | null;
    team_url?: string | null;
    bot_user_id?: string | null;
    bot_name?: string | null;
    app_id?: string | null;
    agent_name?: string | null;
  };
}

export interface SlackConnection {
  /** connecting | connected | reconnecting | auth_error | secret_error | stopped | disconnected */
  state: string;
  connected_at?: string | null;
  last_event_at?: string | null;
  last_error?: string | null;
}

export interface SlackDeletionStep {
  step: "stop" | "revoke_token" | "delete_records" | string;
  ok: boolean;
  error?: string;
}

export interface SlackIntegration {
  harness_id: string;
  integration_id: string;
  /** active | deleting */
  status: string;
  app_id?: string | null;
  team_id?: string | null;
  team_name?: string | null;
  team_url?: string | null;
  bot_user_id?: string | null;
  bot_name?: string | null;
  agent_name?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  deletion?: { at: string; steps: SlackDeletionStep[] } | null;
  connection: SlackConnection;
}

export interface SlackIntegrationSummary {
  harness_id: string;
  status: string;
  team_name?: string | null;
  connection_state?: string | null;
}

export interface SlackManifest {
  json: string;
  yaml: string;
  create_url: string;
  app_scopes: string[];
}

/** A failed call. `detail` is the server's structured body when it sent one. */
export class SlackApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "SlackApiError";
    this.status = status;
    this.detail = detail;
  }

  /** The checks a 400 from test/create carries. */
  get checks(): SlackCheck[] {
    const d = this.detail as { checks?: SlackCheck[] } | null;
    return Array.isArray(d?.checks) ? d!.checks : [];
  }

  /** The per-step results an incomplete delete (502) carries. */
  get steps(): SlackDeletionStep[] {
    const d = this.detail as { steps?: SlackDeletionStep[] } | null;
    return Array.isArray(d?.steps) ? d!.steps : [];
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : `HTTP ${response.status}`;
    throw new SlackApiError(response.status, message, detail);
  }
  return response.json();
}

const path = (harnessId: string) => `/api/harnesses/${encodeURIComponent(harnessId)}/slack`;

export async function listSlackIntegrations(): Promise<SlackIntegrationSummary[]> {
  const data = await request<{ integrations: SlackIntegrationSummary[] }>("/api/slack/integrations");
  return data.integrations;
}

export async function getSlackIntegration(harnessId: string): Promise<SlackIntegration | null> {
  const data = await request<{ integration: SlackIntegration | null }>(path(harnessId));
  return data.integration;
}

export async function getSlackManifest(harnessId: string): Promise<SlackManifest> {
  return request<SlackManifest>(`${path(harnessId)}/manifest`);
}

export async function testSlackTokens(
  harnessId: string,
  botToken: string,
  appToken: string
): Promise<SlackTestResult> {
  return request<SlackTestResult>(`${path(harnessId)}/test`, {
    method: "POST",
    body: JSON.stringify({ bot_token: botToken, app_token: appToken }),
  });
}

export async function createSlackIntegration(
  harnessId: string,
  botToken: string,
  appToken: string
): Promise<SlackIntegration> {
  const data = await request<{ integration: SlackIntegration }>(path(harnessId), {
    method: "POST",
    body: JSON.stringify({ bot_token: botToken, app_token: appToken }),
  });
  return data.integration;
}

export async function deleteSlackIntegration(
  harnessId: string
): Promise<{ deleted: boolean; steps: SlackDeletionStep[] }> {
  return request(path(harnessId), { method: "DELETE" });
}
