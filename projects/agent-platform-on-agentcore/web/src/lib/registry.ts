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
  /**
   * True for the record that points at the server's default runtime. The picker
   * pins it to the top and a new chat with nothing selected starts with it.
   */
  is_default?: boolean;
  /** "deployed" when synthesised from a deployed resource (registry-off fallback). */
  source?: string | null;
  /**
   * True when the discovery data plane serves an APPROVED revision of this record.
   * After an edit `status` is DRAFT but this stays true: the approved revision keeps
   * being served, so chat and search keep working until the new revision is approved.
   */
  discoverable?: boolean | null;
  custom_metadata?: Record<string, string | boolean> | null;
  /** AWS's check of the metadata against the current schema, e.g. after the schema changed. */
  compliance_status?: "COMPLIANT" | "NON_COMPLIANT" | string | null;
  /** True for records created by AWS Organizations auto-detection. */
  auto_detected: boolean;
  source_arn?: string | null;
  /** e.g. "AWS::BedrockAgentCore::Runtime" or "AWS::BedrockAgentCore::Gateway". */
  source_type?: string | null;
}

export interface SyncSource {
  url: string;
  credential: "none" | "iam" | "oauth";
  role_arn?: string;
  provider_arn?: string;
}

export interface RegistryRecordDetail extends RegistryRecordSummary {
  descriptors?: Record<string, unknown> | null;
  descriptor_content?: unknown;
  status_reason?: string | null;
  sync_config?: Record<string, unknown> | null;
  /** Where the registry re-fetches this record's definition from, if it was synced. */
  sync_source?: SyncSource | null;
  provenance?: Array<Record<string, unknown>> | null;
  revision?: "approved" | null;
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

/** A JSON Schema (draft-07, flat) describing one record type's custom metadata. */
export interface MetadataJsonSchema {
  type?: string;
  properties?: Record<string, { type?: string; enum?: string[]; format?: string }>;
  required?: string[];
}

export type MetadataFieldKind = "text" | "enum" | "url" | "boolean";

/** One custom-metadata field, resolved from the schema into a form control. */
export interface MetadataField {
  name: string;
  kind: MetadataFieldKind;
  required: boolean;
  options?: string[];
}

export type CustomMetadataValue = Record<string, string | boolean>;

export interface RegistryInfo {
  registry_id: string;
  name?: string | null;
  description?: string | null;
  status?: string | null;
  auto_approval?: boolean | null;
  registry_arn?: string | null;
  /** e.g. https://agent-registry.us-east-1.api.aws/registry/<id>/mcp */
  mcp_endpoint?: string | null;
  /** Keyed by AWS record type: DEFAULT, MCP, AGENT, SKILL, CUSTOM, GATEWAY. */
  custom_metadata_schema?: Record<string, MetadataJsonSchema> | null;
  auto_detection?: { enabled: boolean; status: "ACTIVE" | "INACTIVE" | null } | null;
  kms_key_arn?: string | null;
  /** When set, the Register dialog may offer it as the sync credential. */
  sync_role_arn?: string | null;
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
  /** Send only when at least one field has a value. */
  custom_metadata?: CustomMetadataValue;
  /** Makes the registry re-fetch the definition from this URL (MCP server or agent card). */
  sync_url?: string;
  /** Credential for `sync_url`; omit for a public endpoint. */
  sync_role_arn?: string;
  tags?: Record<string, string>;
}

export interface UpdateRecordBody {
  name?: string;
  description?: string;
  content?: unknown;
  skill_markdown?: string;
  /** A full replacement map: `{}` clears all metadata. */
  custom_metadata?: CustomMetadataValue;
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
  types?: DescriptorType[],
  meta?: Record<string, string>
): Promise<RegistryRecordSummary[]> {
  const qp = new URLSearchParams({ q: query });
  // Repeated `type` params become the registry's native $in filter server-side.
  for (const type of types ?? []) qp.append("type", type);
  // Search-only: the server turns each `key=value` into a customMetadata $eq filter.
  for (const [key, value] of Object.entries(meta ?? {})) {
    if (value) qp.append("meta", `${key}=${value}`);
  }
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

/**
 * Re-fetch a synced record's definition from its sync URL. The record goes UPDATING
 * and then DRAFT, so it must be submitted again before the new definition is served.
 */
export async function triggerRecordSync(id: string): Promise<RegistryRecordDetail> {
  return request<RegistryRecordDetail>(
    `/api/registry/records/${encodeURIComponent(id)}/sync`,
    { method: "POST" }
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
 * *and* the discovery plane serves an approved revision of it.
 *
 * `discoverable` is what makes this more than `status === "APPROVED"`. AWS keeps
 * two revisions per record: editing an approved record moves its `status` to
 * DRAFT, but the approved revision keeps being served until the edit is approved.
 * Gating on `status` alone would drop an edited agent from every picker while
 * its approved revision is still answering requests. The server applies the same
 * rule when a thread is first bound to the record (403), so hiding the button
 * here is a courtesy, not the boundary. Threads already pinned to a record that
 * later lost approval keep working — the gate only guards new bindings.
 */
export function isChattable(record: RegistryRecordSummary): boolean {
  return (
    Boolean(record.harness_arn || record.agent_runtime_arn) &&
    ((record.status ?? "").toUpperCase() === "APPROVED" ||
      record.discoverable === true)
  );
}

/**
 * The AWS record type a descriptor type is registered as. Metadata schemas are
 * keyed by these, not by our descriptor names. Missing or unknown types fall back
 * to DEFAULT, which is also the schema every type uses when AWS has none of its own.
 */
const AWS_RECORD_TYPE: Record<string, string> = {
  A2A: "AGENT",
  MCP: "MCP",
  AGENT_SKILLS: "SKILL",
  CUSTOM: "CUSTOM",
};

export function recordTypeOf(descriptorType?: string | null): string {
  return (descriptorType && AWS_RECORD_TYPE[descriptorType]) || "DEFAULT";
}

/**
 * The custom-metadata fields a record type accepts, resolved from AWS's schema.
 * Uses the type's own schema when AWS published one, otherwise DEFAULT.
 */
export function metadataSchemaFor(
  schema: Record<string, MetadataJsonSchema> | null | undefined,
  descriptorType?: string | null
): MetadataField[] {
  if (!schema) return [];
  const resolved = schema[recordTypeOf(descriptorType)] ?? schema.DEFAULT;
  if (!resolved?.properties) return [];
  const required = new Set(resolved.required ?? []);

  return Object.entries(resolved.properties).map(([name, prop]): MetadataField => {
    const base = { name, required: required.has(name) };
    if (Array.isArray(prop.enum)) return { ...base, kind: "enum", options: prop.enum };
    if (prop.format === "uri") return { ...base, kind: "url" };
    if (prop.type === "boolean") return { ...base, kind: "boolean" };
    return { ...base, kind: "text" };
  });
}

/**
 * Metadata fields the server fills itself: `owner` is the signed-in user who
 * registered the record, and the server overwrites whatever a client sends for
 * it. The forms never show these; the detail panel still lists their values.
 */
export const SERVER_MANAGED_METADATA_FIELDS: ReadonlySet<string> = new Set(["owner"]);

/**
 * Fields retired from the module's default schema. AWS never lets a saved field
 * be removed, so a registry created before the change still publishes them; the
 * forms hide them and leave any stored value untouched.
 */
export const RETIRED_METADATA_FIELDS: ReadonlySet<string> = new Set(["docs_url"]);

/** The schema fields a person edits in a form: neither server-managed nor retired. */
export function editableMetadataFields(fields: MetadataField[]): MetadataField[] {
  return fields.filter(
    (field) =>
      !SERVER_MANAGED_METADATA_FIELDS.has(field.name) &&
      !RETIRED_METADATA_FIELDS.has(field.name)
  );
}

/**
 * Drops blank text so an untouched optional field is never sent. Returns
 * `undefined` when nothing is left, so the request omits `custom_metadata`
 * entirely instead of sending an empty map.
 *
 * A boolean that was never toggled is absent from the map, so it is not sent
 * either. Toggling it on and then off sends an explicit `false`.
 *
 * Lives here rather than beside the form component: a component module that also
 * exports a function trips the fast-refresh lint rule.
 */
export function compactMetadata(
  value: CustomMetadataValue
): CustomMetadataValue | undefined {
  const out: CustomMetadataValue = {};
  for (const [key, raw] of Object.entries(value)) {
    if (typeof raw === "string") {
      const trimmed = raw.trim();
      if (trimmed) out[key] = trimmed;
    } else {
      out[key] = raw;
    }
  }
  return Object.keys(out).length > 0 ? out : undefined;
}
