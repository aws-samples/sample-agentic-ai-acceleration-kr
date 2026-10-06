/**
 * Client for the per-user knowledge base API at /api/knowledge/*.
 *
 * Provisioning takes minutes and happens in a server background task, so the
 * status values here are a progress report rather than a success/failure pair —
 * the page polls the listing while any of them is in PROVISIONING_STATUSES.
 */

import { authedFetch } from "./http";

export type KnowledgeStatus =
  | "CREATING"
  | "DATA_SOURCE"
  | "GATEWAY"
  | "TARGET"
  | "READY"
  | "CREATE_FAILED"
  | "DELETING"
  | "DELETE_FAILED";

/** Statuses that mean AWS work is still in flight. Mirrors the server set. */
export const PROVISIONING_STATUSES: readonly KnowledgeStatus[] = [
  "CREATING",
  "DATA_SOURCE",
  "GATEWAY",
  "TARGET",
];

/** What Bedrock's managed parser accepts. Mirrors SUPPORTED_MIME_TYPES server-side. */
export const SUPPORTED_MIME_TYPES: Record<string, string> = {
  "text/plain": "Text",
  "text/markdown": "Markdown",
  "text/html": "HTML",
  "text/csv": "CSV",
  "application/pdf": "PDF",
  "application/msword": "Word (.doc)",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
    "Word (.docx)",
};

/**
 * For <input accept="...">. Extensions as well as MIME types because browsers
 * report .md as text/plain (or ""), and an accept list of MIME types alone would
 * grey out markdown files the server accepts.
 */
export const ACCEPT_ATTRIBUTE = [
  ...Object.keys(SUPPORTED_MIME_TYPES),
  ".md",
  ".markdown",
  ".txt",
  ".csv",
  ".html",
  ".pdf",
  ".doc",
  ".docx",
].join(",");

/** Matches MAX_UPLOAD_BYTES server-side; checked here to fail before the upload. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/**
 * Where a knowledge base's documents come from. Fixed at creation — AWS does not
 * allow a data source to change connector type.
 *
 * UPLOAD indexes each file as it arrives and has no sync. S3 reads a bucket the
 * organisation already owns, so it syncs and cannot be uploaded to. The server
 * rejects the wrong verb either way; hiding the button is a courtesy, not the rule.
 */
export type KnowledgeSourceType = "UPLOAD" | "S3";

export interface S3SourceConfig {
  bucket_name: string;
  prefix?: string | null;
  managed?: boolean;
}

export interface KnowledgeBaseRecord {
  kb_key: string;
  owner_id: string;
  owner_name: string;
  /** Admin-created and visible to everyone. */
  shared: boolean;
  name: string;
  description?: string | null;
  /** Absent on knowledge bases created before source types existed. */
  source_type?: KnowledgeSourceType | null;
  source_config?: Partial<S3SourceConfig> | null;
  status: KnowledgeStatus | string;
  failure_reason?: string | null;
  kb_id?: string | null;
  data_source_id?: string | null;
  gateway_id?: string | null;
  /** What the harness composer passes in `gateway_arns`. Absent until READY. */
  gateway_arn?: string | null;
  target_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocument {
  doc_id: string;
  filename: string;
  /** IN_PROGRESS, INDEXED, FAILED, ... straight from ListKnowledgeBaseDocuments. */
  status?: string | null;
  status_reason?: string | null;
  updated_at?: string | null;
}

export interface SyncJob {
  job_id: string;
  /** STARTING, IN_PROGRESS, COMPLETE, FAILED, STOPPING, STOPPED. */
  status: string;
  started_at?: string | null;
  updated_at?: string | null;
  documents_scanned: number;
  documents_indexed: number;
  documents_modified: number;
  documents_deleted: number;
  documents_failed: number;
  documents_skipped: number;
  failure_reasons: string[];
}

export interface KnowledgeBaseDetail {
  knowledge_base: KnowledgeBaseRecord;
  documents: KnowledgeDocument[];
  /** Null for UPLOAD, and for a source that has never been synced. */
  sync?: SyncJob | null;
}

export interface CreateKnowledgeBaseBody {
  name: string;
  description?: string;
  /** Admin-only server-side; a non-admin request is rejected with 403. */
  shared?: boolean;
  source_type?: KnowledgeSourceType;
  source_config?: Partial<S3SourceConfig>;
}

/** UPLOAD is the default, including for records that predate the field. */
export function sourceTypeOf(kb: KnowledgeBaseRecord): KnowledgeSourceType {
  return kb.source_type === "S3" ? "S3" : "UPLOAD";
}

export function isUploadSource(kb: KnowledgeBaseRecord): boolean {
  return sourceTypeOf(kb) === "UPLOAD";
}

/**
 * An S3 source whose bucket and prefix the server assigned — the platform owns
 * the objects, so files can be uploaded and deleted from the site. External
 * (admin-attached) S3 sources are read-only and sync-only.
 */
export function isManagedSource(kb: KnowledgeBaseRecord): boolean {
  return sourceTypeOf(kb) === "S3" && kb.source_config?.managed === true;
}

/** Whether the site can add and remove this knowledge base's files. */
export function canManageFiles(kb: KnowledgeBaseRecord): boolean {
  return isUploadSource(kb) || isManagedSource(kb);
}

/**
 * The source type as a person would name it. Three values rather than the two
 * of KnowledgeSourceType because managed and external S3 behave differently
 * enough (writable vs read-only) that a card should say which one it is.
 */
export function sourceTypeLabel(kb: KnowledgeBaseRecord): string {
  if (sourceTypeOf(kb) !== "S3") return "업로드";
  return isManagedSource(kb) ? "관리형 S3" : "외부 S3";
}

/** `s3://bucket/prefix` for an S3 source, else a label for uploaded files. */
export function sourceLabel(kb: KnowledgeBaseRecord): string {
  if (sourceTypeOf(kb) !== "S3") return "업로드한 파일";
  const bucket = kb.source_config?.bucket_name || "";
  const prefix = kb.source_config?.prefix || "";
  return prefix ? `s3://${bucket}/${prefix}` : `s3://${bucket}`;
}

/**
 * Statuses a sync job never moves off of. A list of endings rather than of
 * middles, for the same reason the document poll uses one: AWS ships status
 * values ahead of its own SDK enum.
 */
const SYNC_TERMINAL = new Set(["COMPLETE", "FAILED", "STOPPED"]);

export function isSyncing(job?: SyncJob | null): boolean {
  return job != null && !SYNC_TERMINAL.has((job.status || "").toUpperCase());
}

export function isProvisioning(kb: KnowledgeBaseRecord): boolean {
  return PROVISIONING_STATUSES.includes(kb.status as KnowledgeStatus);
}

/** Human-readable progress. The raw statuses name internal AWS steps. */
export function statusLabel(kb: KnowledgeBaseRecord): string {
  switch (kb.status) {
    case "CREATING":
      return "Knowledge Base 생성 중";
    case "DATA_SOURCE":
      return "데이터 소스 연결 중";
    case "GATEWAY":
      return "Gateway 생성 중";
    case "TARGET":
      return "MCP 연결 중";
    case "READY":
      return "사용 가능";
    case "CREATE_FAILED":
      return "생성 실패";
    case "DELETING":
      return "삭제 중";
    case "DELETE_FAILED":
      return "삭제 실패";
    default:
      return kb.status;
  }
}

/**
 * An API error that keeps its status code.
 *
 * The delete flow has to tell "still attached to a harness" (409, offer to force)
 * apart from a plain validation failure (400), and matching on the message text
 * would break the moment the wording changes.
 */
export class KnowledgeApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "KnowledgeApiError";
    this.status = status;
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function fail(response: Response): Promise<never> {
  const err = await response
    .json()
    .catch(() => ({ detail: response.statusText }));
  throw new KnowledgeApiError(
    response.status,
    err.detail || `HTTP ${response.status}`
  );
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, options);
  if (!response.ok) await fail(response);
  return response.json();
}

export async function listKnowledgeBases(
  status?: KnowledgeStatus
): Promise<KnowledgeBaseRecord[]> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await request<{ knowledge_bases: KnowledgeBaseRecord[] }>(
    `/api/knowledge${suffix}`
  );
  return data.knowledge_bases;
}

export async function createKnowledgeBase(
  body: CreateKnowledgeBaseBody
): Promise<KnowledgeBaseRecord> {
  return request<KnowledgeBaseRecord>("/api/knowledge", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getKnowledgeBase(
  kbKey: string
): Promise<KnowledgeBaseDetail> {
  return request<KnowledgeBaseDetail>(
    `/api/knowledge/${encodeURIComponent(kbKey)}`
  );
}

export async function deleteKnowledgeBase(
  kbKey: string,
  force = false
): Promise<void> {
  await request<{ deleted: string }>(
    `/api/knowledge/${encodeURIComponent(kbKey)}${force ? "?force=true" : ""}`,
    { method: "DELETE" }
  );
}

/**
 * Upload one file. Multipart, so no Content-Type is set: the browser has to
 * supply its own boundary, and overriding it makes FastAPI reject the body.
 */
export async function uploadKnowledgeDocument(
  kbKey: string,
  file: File
): Promise<KnowledgeDocument> {
  const form = new FormData();
  form.append("file", file);

  const response = await authedFetch(
    `${API_BASE}/api/knowledge/${encodeURIComponent(kbKey)}/documents`,
    { method: "POST", body: form },
    true
  );
  if (!response.ok) await fail(response);
  return response.json();
}

export async function deleteKnowledgeDocument(
  kbKey: string,
  docId: string
): Promise<void> {
  await request<{ deleted: string }>(
    `/api/knowledge/${encodeURIComponent(kbKey)}/documents/${encodeURIComponent(docId)}`,
    { method: "DELETE" }
  );
}

export interface SourceBucketsInfo {
  /** External buckets an admin may attach; always empty for non-admins. */
  buckets: string[];
  /** Whether this environment has a platform bucket for managed S3 sources. */
  platform_available: boolean;
}

/**
 * What the create dialog needs to offer S3 sources. `buckets` drives the
 * admin-only external picker; `platform_available` is the only signal that the
 * S3 option can work at all in this environment.
 */
export async function listSourceBuckets(): Promise<SourceBucketsInfo> {
  const data = await request<Partial<SourceBucketsInfo>>(
    "/api/knowledge/source-buckets"
  );
  return {
    buckets: data.buckets ?? [],
    platform_available: data.platform_available ?? false,
  };
}

/** Start a sync, or return the one already running. */
export async function syncKnowledgeBase(kbKey: string): Promise<SyncJob> {
  return request<SyncJob>(
    `/api/knowledge/${encodeURIComponent(kbKey)}/sync`,
    { method: "POST" }
  );
}
