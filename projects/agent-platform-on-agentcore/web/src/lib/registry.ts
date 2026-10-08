/**
 * Client for the AgentCore Agent Registry API exposed by FastAPI at /api/registry/*.
 */

import { authedFetch } from "@/lib/http";

export type DescriptorType = "A2A" | "AGENT_SKILLS" | "MCP" | "CUSTOM";
export type StatusAction = "submit" | "approve" | "reject" | "deprecate";

export interface RegistryRecordSummary {
  record_id: string;
  name: string;
  description?: string | null;
  descriptor_type?: string | null;
  version?: string | null;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  record_arn?: string | null;
  agent_runtime_arn?: string | null;
  /** Set instead of agent_runtime_arn for managed-harness agents. */
  harness_arn?: string | null;
  qualifier?: string | null;
  /** "deployed" when synthesised from a deployed resource (registry-off fallback). */
  source?: string | null;
}

export interface RegistryRecordDetail extends RegistryRecordSummary {
  descriptors?: Record<string, unknown> | null;
  descriptor_content?: unknown;
  status_reason?: string | null;
  sync_config?: Record<string, unknown> | null;
}

export interface AgentRuntimeSummary {
  name: string;
  agent_runtime_arn: string;
  status?: string | null;
  description?: string | null;
}

export interface GatewaySummary {
  name: string;
  gateway_id: string;
  gateway_arn: string;
  gateway_url?: string | null;
  status?: string | null;
  description?: string | null;
  /** AWS_IAM or CUSTOM_JWT. */
  authorizer_type?: string | null;
}

/**
 * A deployed AgentCore runtime, harness or gateway, paired with its record.
 *
 * A gateway is a tool surface rather than an agent: it registers as an MCP record
 * so the harness composer can attach it, and never appears in the chat picker.
 */
export interface DeployedTarget {
  kind: "runtime" | "harness" | "gateway";
  name: string;
  arn: string;
  status?: string | null;
  description?: string | null;
  registered: boolean;
  /**
   * The only record for this deployment was deprecated. Deprecation is terminal
   * in AWS, so re-registering creates a new record — a bulk sync skips these and
   * they must be registered explicitly.
   */
  retired: boolean;
  record_id?: string | null;
  record_name?: string | null;
  record_status?: string | null;
  runtime_arn?: string | null;
  /** MCP endpoint, for gateway targets. */
  gateway_url?: string | null;
  /** Set when the target can't be registered as-is (e.g. not READY yet). */
  reason?: string | null;
}

export interface SyncAgentsResponse {
  registered: RegistryRecordSummary[];
  skipped: DeployedTarget[];
  failed: Array<{ name: string; arn?: string | null; error: string }>;
}

export interface RegistryInfo {
  registry_id: string;
  name?: string | null;
  description?: string | null;
  status?: string | null;
  auto_approval?: boolean | null;
}

export interface CreateRecordBody {
  name: string;
  description?: string;
  descriptor_type: DescriptorType;
  version?: string;
  agent_runtime_arn?: string;
  harness_arn?: string;
  qualifier?: string;
  /** MCP endpoint URL, so the record can be composed as a harness tool. */
  remote_url?: string;
  /**
   * For an MCP record backed by an AgentCore gateway. The composer attaches these
   * as a native gateway tool, which signs the gateway's required SigV4.
   */
  gateway_arn?: string;
  content?: unknown;
  skill_markdown?: string;
  submit_for_approval?: boolean;
}

export interface UpdateRecordBody {
  name?: string;
  description?: string;
  content?: unknown;
  skill_markdown?: string;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * A rejected registry call, with the status kept.
 *
 * Callers have to tell a record that is gone from a server that was momentarily
 * busy, and the API reports both as 502 (a deleted record arrives as 502 wrapping
 * ResourceNotFoundException, not as 404). A bare `Error` erased the status, so the
 * only distinguishing signal left was the message text. `message` is unchanged, so
 * existing `catch` blocks that read it keep working.
 */
export class RegistryRequestError extends Error {
  readonly status?: number;
  readonly detail: string;

  constructor(detail: string, status?: number) {
    super(detail);
    this.name = "RegistryRequestError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, options);
  if (!response.ok) {
    const err = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new RegistryRequestError(
      err.detail || `HTTP ${response.status}`,
      response.status
    );
  }
  return response.json();
}

export async function getRegistryInfo(): Promise<RegistryInfo> {
  return request<RegistryInfo>("/api/registry/info");
}

export async function listRegistryRecords(params?: {
  type?: DescriptorType;
  status?: string;
  name?: string;
}): Promise<RegistryRecordSummary[]> {
  const qp = new URLSearchParams();
  if (params?.type) qp.set("type", params.type);
  if (params?.status) qp.set("status", params.status);
  if (params?.name) qp.set("name", params.name);
  const suffix = qp.toString() ? `?${qp.toString()}` : "";
  const data = await request<{ records: RegistryRecordSummary[] }>(
    `/api/registry/records${suffix}`
  );
  return data.records;
}

export async function searchRegistryRecords(
  query: string,
  types?: DescriptorType[]
): Promise<RegistryRecordSummary[]> {
  const qp = new URLSearchParams({ q: query });
  // Repeated `type` params become the registry's native $in filter server-side.
  for (const type of types ?? []) qp.append("type", type);
  const data = await request<{ records: RegistryRecordSummary[] }>(
    `/api/registry/search?${qp.toString()}`
  );
  return data.records;
}

export async function getRegistryRecord(id: string): Promise<RegistryRecordDetail> {
  return request<RegistryRecordDetail>(
    `/api/registry/records/${encodeURIComponent(id)}`
  );
}

export async function listAgentRuntimes(): Promise<AgentRuntimeSummary[]> {
  const data = await request<{ runtimes: AgentRuntimeSummary[] }>(
    "/api/registry/runtimes"
  );
  return data.runtimes;
}

export async function listGateways(): Promise<GatewaySummary[]> {
  const data = await request<{ gateways: GatewaySummary[] }>(
    "/api/registry/gateways"
  );
  return data.gateways;
}

export async function listDeployedTargets(): Promise<{
  targets: DeployedTarget[];
  unregistered: number;
  retired: number;
}> {
  const data = await request<{
    targets: DeployedTarget[];
    unregistered: number;
    retired: number;
  }>("/api/registry/deployed");
  return {
    targets: data.targets,
    unregistered: data.unregistered,
    retired: data.retired,
  };
}

/** Register deployed agents that have no record yet; empty `targets` means all. */
export async function syncDeployedAgents(
  targets: string[] = []
): Promise<SyncAgentsResponse> {
  return request<SyncAgentsResponse>("/api/registry/sync", {
    method: "POST",
    body: JSON.stringify({ targets, submit_for_approval: true }),
  });
}

export async function createRegistryRecord(
  body: CreateRecordBody
): Promise<RegistryRecordSummary> {
  return request<RegistryRecordSummary>("/api/registry/records", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateRegistryRecord(
  id: string,
  body: UpdateRecordBody
): Promise<RegistryRecordDetail> {
  return request<RegistryRecordDetail>(
    `/api/registry/records/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify(body) }
  );
}

export async function updateRegistryRecordStatus(
  id: string,
  action: StatusAction
): Promise<RegistryRecordSummary> {
  return request<RegistryRecordSummary>(
    `/api/registry/records/${encodeURIComponent(id)}/status`,
    { method: "POST", body: JSON.stringify({ action }) }
  );
}

export async function deleteRegistryRecord(id: string): Promise<void> {
  await request(`/api/registry/records/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/**
 * A record is chattable when it resolves to a harness or a deployed runtime
 * *and* has passed curation. The server enforces the same rule when a thread
 * is first bound to the record (403), so hiding the button here is a courtesy,
 * not the boundary. Threads already pinned to a record that later lost
 * approval keep working — the gate only guards new bindings.
 */
export function isChattable(record: RegistryRecordSummary): boolean {
  return (
    Boolean(record.harness_arn || record.agent_runtime_arn) &&
    (record.status ?? "").toUpperCase() === "APPROVED"
  );
}
