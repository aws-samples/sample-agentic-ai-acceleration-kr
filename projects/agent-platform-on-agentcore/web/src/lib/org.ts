/**
 * Organization page API: teams the server provisions, people in the pool,
 * drift and the audit ledger. Every route is admin-only; a 403 surfaces as
 * OrgApiError(403) and the page renders "관리자만".
 */
import { authedFetch } from "./http";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export type TeamStatus = "provisioning" | "ready" | "error" | "deleting";

export interface OrgTeam {
  name: string;
  label: string;
  description: string;
  execution_role_arn: string;
  allowed_models: string[];
  allowed_tools: string[];
  daily_cost_alert_usd: number | null;
  /** "platform" = the server made the role and policies; any other value = written from outside (read-only lifecycle). */
  managed_by: string;
  status: TeamStatus;
  error: { step: string; message: string; at?: string } | null;
  policy_ids: Record<string, string>;
  entra_group_ids: string[];
  created_by: string;
  created_at: string;
  updated_by: string;
  updated_at: string;
  members: number;
  turns_30d: number;
  model_cost_micros_30d: number;
  /** Today's model cost (business date, still being written); null when the ledger was unreadable. */
  today_cost_micros: number | null;
  policy_linked: boolean;
}

export interface OrgTeamsResponse {
  teams: OrgTeam[];
  persisted: boolean;
  provisioner_enabled: boolean;
  policies_enabled: boolean;
}

export interface TeamCreateBody {
  name: string;
  label?: string;
  description?: string;
  allowed_models?: string[];
  allowed_tools?: string[];
  daily_cost_alert_usd?: number | null;
  entra_group_ids?: string[];
}

export type TeamUpdateBody = Partial<Omit<TeamCreateBody, "name">>;

export interface OrgUser {
  sub: string;
  username: string;
  email: string;
  name: string;
  provider: string;
  role: "admin" | "user";
  teams: string[];
  status: string;
  enabled: boolean;
  created_at: string;
  last_login: string;
  groups: string[];
  turns_30d: number;
  model_cost_micros_30d: number;
}

export interface UserFilter {
  q?: string;
  team?: string;
  role?: string;
  provider?: string;
  status?: string;
  refresh?: boolean;
}

export interface InviteResult {
  invited: { sub: string; email: string; teams: string[]; role: string; temporary_password?: string }[];
  failed: { email: string; detail: string }[];
}

export interface DriftItem {
  kind: string;
  team: string;
  detail: string;
  actions: string[];
}

export interface AuditEntry {
  id: string;
  at: string;
  actor_sub: string;
  actor_email: string;
  action: string;
  target: string;
  before: unknown;
  after: unknown;
  result: string;
  message: string;
}

export class OrgApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "OrgApiError";
    this.status = status;
  }
}

/** Every /api/org route is 501 without TEAMS_TABLE; each tab says so the same way. */
export function orgErrorMessage(e: unknown): string {
  if (e instanceof OrgApiError && e.status === 501) return "이 배포에는 TEAMS_TABLE 이 없어 조직 관리를 쓸 수 없습니다.";
  return e instanceof Error ? e.message : String(e);
}

async function request<T>(endpoint: string, init?: RequestInit): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new OrgApiError(response.status, err.detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

const enc = encodeURIComponent;

export const fetchOrgTeams = () => request<OrgTeamsResponse>("/api/org/teams");
export const fetchOrgTeam = (name: string) => request<OrgTeam>(`/api/org/teams/${enc(name)}`);
export const createOrgTeam = (body: TeamCreateBody) =>
  request<OrgTeam>("/api/org/teams", { method: "POST", body: JSON.stringify(body) });
export const updateOrgTeam = (name: string, body: TeamUpdateBody) =>
  request<OrgTeam>(`/api/org/teams/${enc(name)}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteOrgTeam = (name: string) => request<void>(`/api/org/teams/${enc(name)}`, { method: "DELETE" });
export const provisionOrgTeam = (name: string) =>
  request<OrgTeam>(`/api/org/teams/${enc(name)}/provision`, { method: "POST" });
export const fetchTeamMembers = (name: string) =>
  request<{ members: OrgUser[]; sources: Record<string, string> }>(`/api/org/teams/${enc(name)}/members`);
export const addTeamMember = (name: string, sub: string) =>
  request<void>(`/api/org/teams/${enc(name)}/members/${enc(sub)}`, { method: "PUT" });
export const removeTeamMember = (name: string, sub: string) =>
  request<void>(`/api/org/teams/${enc(name)}/members/${enc(sub)}`, { method: "DELETE" });
export type TeamRecord = { record_id: string; name: string; descriptor_type?: string | null };
export const fetchTeamRecords = (name: string) =>
  request<{ records: TeamRecord[] }>(`/api/org/teams/${enc(name)}/records`);

export function fetchOrgUsers(params: UserFilter = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "" && v !== false) qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<{ users: OrgUser[]; total: number; sources: Record<string, string> }>(`/api/org/users${suffix}`);
}
export const inviteUsers = (body: { emails: string[]; teams: string[]; role: string; send_email: boolean }) =>
  request<InviteResult>("/api/org/users", { method: "POST", body: JSON.stringify(body) });
export const fetchOrgUser = (sub: string) => request<OrgUser>(`/api/org/users/${enc(sub)}`);
export const updateOrgUser = (sub: string, body: { role?: string; teams?: string[]; enabled?: boolean }) =>
  request<OrgUser>(`/api/org/users/${enc(sub)}`, { method: "PATCH", body: JSON.stringify(body) });
export const resetUserPassword = (sub: string) =>
  request<void>(`/api/org/users/${enc(sub)}/reset-password`, { method: "POST" });
export const deleteOrgUser = (sub: string) => request<void>(`/api/org/users/${enc(sub)}`, { method: "DELETE" });

export const fetchDrift = () => request<{ items: DriftItem[]; checked_at: string }>("/api/org/drift");
export function fetchAudit(params: { from?: string; to?: string; actor?: string; target?: string } = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
  const suffix = qs.toString() ? `?${qs}` : "";
  return request<{ entries: AuditEntry[]; from: string; to: string }>(`/api/org/audit${suffix}`);
}
export const fetchGatewayTools = () =>
  request<{ tools: string[]; shared: string[]; conditioned: string[] }>("/api/org/tools");
