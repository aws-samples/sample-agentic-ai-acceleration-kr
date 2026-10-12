/**
 * Client for the skill bundle endpoints at /api/registry/skills/*.
 *
 * A skill is a directory, and the registry record can only hold SKILL.md inline —
 * so the bundle is uploaded here, validated against the AgentSkills spec, and
 * published to S3, and the record ends up pointing at that prefix. Nobody types an
 * S3 URI.
 */

import { authedFetch } from "@/lib/http";
import { RegistryRequestError, type RegistryRecordDetail, type RegistryRecordSummary } from "@/lib/registry";

export interface SkillFile {
  path: string;
  size_bytes: number;
}

/** The outcome of validating an upload. `errors` non-empty means unpublishable. */
export interface SkillBundleInfo {
  name: string;
  description: string;
  skill_md: string;
  files: SkillFile[];
  total_bytes: number;
  errors: string[];
  /** Spec deviations that still work — unconventional directory, long SKILL.md. */
  warnings: string[];
  license?: string | null;
  compatibility?: string | null;
  metadata: Record<string, string>;
  allowed_tools?: string | null;
}

export interface SkillFilesResponse {
  /** null for a record whose SKILL.md was entered inline, before uploads existed. */
  uri: string | null;
  files: SkillFile[];
  total_bytes: number;
}

/** Extensions the upload accepts: one markdown file, or a zipped skill directory. */
export const SKILL_UPLOAD_ACCEPT = ".md,.markdown,.zip";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * A multipart request. `omitContentType` is required: the browser has to set the
 * multipart boundary itself, and a hand-written `Content-Type` would replace it
 * with one that has no boundary at all.
 */
async function send<T>(endpoint: string, method: string, file: File, fields?: Record<string, string>): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(fields ?? {})) {
    form.append(key, value);
  }

  const response = await authedFetch(
    `${API_BASE}${endpoint}`,
    { method, body: form },
    true
  );
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

/** Check a bundle without storing it. Invalid bundles come back as `errors`. */
export async function validateSkillBundle(file: File): Promise<SkillBundleInfo> {
  return send<SkillBundleInfo>("/api/registry/skills/validate", "POST", file);
}

export async function createSkillRecord(
  file: File,
  options?: { version?: string; submitForApproval?: boolean }
): Promise<RegistryRecordSummary> {
  const fields: Record<string, string> = {
    submit_for_approval: String(options?.submitForApproval ?? true),
  };
  if (options?.version) fields.version = options.version;
  return send<RegistryRecordSummary>("/api/registry/skills", "POST", file, fields);
}

/** Republish an existing skill's bundle. On an APPROVED record this drafts a revision. */
export async function replaceSkillBundle(
  recordId: string,
  file: File
): Promise<RegistryRecordDetail> {
  return send<RegistryRecordDetail>(
    `/api/registry/skills/${encodeURIComponent(recordId)}`,
    "PUT",
    file
  );
}

export async function listSkillFiles(recordId: string): Promise<SkillFilesResponse> {
  const response = await authedFetch(
    `${API_BASE}/api/registry/skills/${encodeURIComponent(recordId)}/files`
  );
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

// --- bucket-only skills (registry-off) ------------------------------------
//
// When the registry is off there is no AGENT_SKILLS record, but the bundle still
// lives in SKILLS_BUCKET so the harness picker can offer it. These hit the
// bucket-only routes: publish without a record, list what is there, delete a prefix.

/** A skill bundle discovered directly in SKILLS_BUCKET (needs no registry). */
export interface BucketSkill {
  name: string;
  description?: string | null;
  uri: string;
}

export async function listBucketSkills(): Promise<BucketSkill[]> {
  const response = await authedFetch(`${API_BASE}/api/registry/skills/bucket`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new RegistryRequestError(err.detail || `HTTP ${response.status}`, response.status);
  }
  const data = (await response.json()) as { skills: BucketSkill[] };
  return data.skills;
}

/** Publish a bundle straight to the bucket, creating no registry record. */
export async function publishBucketSkill(file: File): Promise<BucketSkill> {
  return send<BucketSkill>("/api/registry/skills/bucket", "POST", file);
}

export async function deleteBucketSkill(
  uri: string
): Promise<{ deleted: string; removed: number }> {
  const response = await authedFetch(
    `${API_BASE}/api/registry/skills/bucket?uri=${encodeURIComponent(uri)}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new RegistryRequestError(err.detail || `HTTP ${response.status}`, response.status);
  }
  return response.json();
}

/** Wrap pasted SKILL.md text as an upload, so both entry styles hit one endpoint. */
export function skillMarkdownFile(markdown: string): File {
  return new File([markdown], "SKILL.md", { type: "text/markdown" });
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
