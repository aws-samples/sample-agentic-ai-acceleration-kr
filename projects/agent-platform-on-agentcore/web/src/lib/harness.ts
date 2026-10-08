/**
 * Client for the AgentCore Managed Agent Harness API at /api/harnesses/*.
 */

import { authedFetch } from "@/lib/http";
import type { RegistryRecordSummary } from "./registry";
import type { BucketSkill } from "./skills";

export interface ComposableRecord {
  record_id: string;
  name: string;
  description?: string | null;
  descriptor_type: string;
  status?: string | null;
  composable: boolean;
  reason?: string | null;
  // What the record resolves to on a harness. The edit form maps an existing
  // harness's tools/skills back onto catalogue rows by these.
  url?: string | null;
  gateway_arn?: string | null;
  s3_uri?: string | null;
}

export interface TruncationSettings {
  strategy: "sliding_window" | "summarization" | "none";
  /** sliding_window only; AWS defaults to 150. */
  messages_count?: number | null;
}

export interface MemorySettings {
  arn?: string | null;
  strategies: string[];
  event_expiry_days?: number | null;
  disabled: boolean;
}

export interface HarnessCatalog {
  mcp_servers: ComposableRecord[];
  skills: ComposableRecord[];
  // Bundles found in the skills bucket; the registry-off skill picker's source.
  bucket_skills: BucketSkill[];
  aws_skill_categories: Array<{ path: string; label: string }>;
  builtin_tools: string[];
  default_model_id: string;
  configured: boolean;
}

export interface HarnessSummary {
  harness_id: string;
  harness_arn: string;
  harness_name: string;
  status?: string | null;
  runtime_arn?: string | null;
  failure_reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  model_id?: string | null;
  tools?: Array<Record<string, unknown>> | null;
  skills?: Array<Record<string, unknown>> | null;
  // GetHarness-only detail for the edit form; absent on a listing row.
  version?: string | null;
  system_prompt?: string | null;
  max_tokens?: number | null;
  max_iterations?: number | null;
  timeout_seconds?: number | null;
  allowed_tools?: string[] | null;
  truncation?: TruncationSettings | null;
  memory?: MemorySettings | null;
  gateway_arns?: string[] | null;
  mcp_urls?: string[] | null;
  builtin_tools?: string[] | null;
  skill_uris?: string[] | null;
  aws_skill_paths?: string[] | null;
}

export interface ComposeHarnessBody {
  name: string;
  description?: string;
  system_prompt?: string;
  model_id?: string;
  mcp_record_ids: string[];
  skill_record_ids: string[];
  // Uploaded skill bundles chosen by S3 prefix — the registry-off skill source.
  skill_bucket_uris?: string[];
  // The AWS Agent Toolkit skill catalog is no longer surfaced in the compose
  // UI; the backend still accepts these paths (defaulting to none).
  aws_skill_paths?: string[];
  builtin_tools: string[];
  gateway_arns: string[];
  allowed_tools?: string[];
  max_iterations?: number;
  timeout_seconds?: number;
  max_tokens?: number;
  truncation?: TruncationSettings;
  /** Create only: memory is never re-sent on update. */
  memory_event_expiry_days?: number;
}

/**
 * Everything the edit form may change. UpdateHarness is a partial update, so an
 * omitted field is left alone; the form sends its whole selection so the tool
 * and skill lists (which the API replaces) reflect exactly what is ticked.
 * No name (immutable), no description (lives on the registry record), no memory.
 */
export type UpdateHarnessBody = Omit<
  ComposeHarnessBody,
  "name" | "description" | "memory_event_expiry_days"
>;

export interface ComposeHarnessResponse {
  harness: HarnessSummary;
  record?: RegistryRecordSummary | null;
  warning?: string | null;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, options);
  if (!response.ok) {
    const err = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export async function getHarnessCatalog(): Promise<HarnessCatalog> {
  return request<HarnessCatalog>("/api/harnesses/catalog");
}

export async function listHarnesses(): Promise<HarnessSummary[]> {
  const data = await request<{ harnesses: HarnessSummary[] }>("/api/harnesses");
  return data.harnesses;
}

export async function getHarness(harnessId: string): Promise<HarnessSummary> {
  return request<HarnessSummary>(
    `/api/harnesses/${encodeURIComponent(harnessId)}`
  );
}

/** Harness ARNs end in the harness id, which is what the control API takes. */
export function harnessIdFromArn(arn: string): string | null {
  const id = arn.split("/").pop();
  return id || null;
}

export async function composeHarness(
  body: ComposeHarnessBody
): Promise<ComposeHarnessResponse> {
  return request<ComposeHarnessResponse>("/api/harnesses", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Edit in place. The ARN is unchanged, so the registry record stays bound;
 * AgentCore versions the definition and the DEFAULT endpoint follows once the
 * new version is READY. Answers the UPDATING summary — poll `getHarness`.
 */
export async function updateHarness(
  harnessId: string,
  body: UpdateHarnessBody
): Promise<HarnessSummary> {
  return request<HarnessSummary>(
    `/api/harnesses/${encodeURIComponent(harnessId)}`,
    { method: "PUT", body: JSON.stringify(body) }
  );
}

/** Deleting also deprecates the registry record bound to the harness. */
export async function deleteHarness(
  harnessId: string
): Promise<{ deleted: string; deprecated_records: string[] }> {
  return request<{ deleted: string; deprecated_records: string[] }>(
    `/api/harnesses/${encodeURIComponent(harnessId)}`,
    { method: "DELETE" }
  );
}

/** HarnessName is stricter than registry record names: no hyphens or dots. */
export const HARNESS_NAME_PATTERN = /^[a-zA-Z][a-zA-Z0-9_]{0,39}$/;

export function suggestHarnessName(name: string): string {
  return name
    .trim()
    .replace(/[^a-zA-Z0-9_]/g, "_")
    .replace(/^[_0-9]+/, "")
    .slice(0, 40);
}
