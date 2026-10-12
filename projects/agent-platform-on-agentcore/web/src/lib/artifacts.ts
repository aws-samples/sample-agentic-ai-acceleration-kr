/**
 * Client for the artifact API exposed by FastAPI at /api/artifacts/*.
 */

import { authedFetch } from "@/lib/http";

export type ArtifactKind =
  | "markdown"
  | "code"
  | "html"
  | "svg"
  | "mermaid"
  | "csv"
  | "json"
  | "text"
  /** A binary file produced in a harness sandbox: no inline body, download only. */
  | "file";

/** The `artifact` stream event, after the server resolved id and version. */
export interface ArtifactEvent {
  artifactId: string;
  version: number;
  threadId?: string;
  title: string;
  kind: ArtifactKind;
  language?: string;
  /** Inline body. Absent for versions loaded from storage — fetch on demand. */
  content?: string;
  filename?: string;
  toolCallId?: string;
  messageId?: string;
  s3Key?: string;
  sizeBytes?: number;
  createdAt?: string;
  /** False when artifact storage is unconfigured or the write failed. */
  stored?: boolean;
  storeError?: string;
}

export interface ArtifactVersion {
  artifact_id: string;
  version: number;
  thread_id: string;
  title: string;
  kind: ArtifactKind;
  language?: string | null;
  s3_key: string;
  size_bytes: number;
  created_at: string;
  tool_call_id?: string | null;
  message_id?: string | null;
  filename?: string | null;
  content_type?: string | null;
  preview_key?: string | null;
}

export interface ArtifactDetail {
  latest: ArtifactVersion;
  versions: number[];
}

export interface ArtifactContent {
  artifact_id: string;
  version: number;
  kind: ArtifactKind;
  language?: string | null;
  title: string;
  content: string;
}

export interface ArtifactShare {
  artifact_id: string;
  version: number;
  url: string;
  expires_in: number;
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

export async function listThreadArtifacts(
  threadId: string
): Promise<ArtifactVersion[]> {
  return request<ArtifactVersion[]>(
    `/api/artifacts/thread/${encodeURIComponent(threadId)}`
  );
}

export async function getArtifact(artifactId: string): Promise<ArtifactDetail> {
  return request<ArtifactDetail>(
    `/api/artifacts/${encodeURIComponent(artifactId)}`
  );
}

export async function getArtifactContent(
  artifactId: string,
  version: number
): Promise<ArtifactContent> {
  return request<ArtifactContent>(
    `/api/artifacts/${encodeURIComponent(artifactId)}/versions/${version}/content`
  );
}

export async function shareArtifact(
  artifactId: string,
  version: number,
  options?: { download?: boolean; expiresIn?: number }
): Promise<ArtifactShare> {
  const qp = new URLSearchParams();
  if (options?.download) qp.set("download", "true");
  if (options?.expiresIn) qp.set("expires_in", String(options.expiresIn));
  const suffix = qp.toString() ? `?${qp.toString()}` : "";
  return request<ArtifactShare>(
    `/api/artifacts/${encodeURIComponent(
      artifactId
    )}/versions/${version}/share${suffix}`,
    { method: "POST" }
  );
}

export interface ArtifactPreview {
  media: "markdown" | "sheets" | "csv" | "text" | "html" | "json";
  text: string;
}

/** The extracted preview, or null when the format has none. */
export async function fetchArtifactPreview(
  artifactId: string,
  version: number,
): Promise<ArtifactPreview | null> {
  const response = await authedFetch(
    `${API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/versions/${version}/preview`,
  );
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Preview failed: ${response.status}`);
  return response.json();
}

/** The artifact's bytes, fetched with credentials. */
export async function fetchArtifactFile(
  artifactId: string,
  version: number,
): Promise<{ blob: Blob; filename: string }> {
  const response = await authedFetch(
    `${API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/versions/${version}/download`,
  );
  if (!response.ok) throw new Error(`Download failed: ${response.status}`);
  // The server sends RFC 5987 `filename*`; fall back to the artifact id when a
  // proxy strips it.
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
  return {
    blob: await response.blob(),
    filename: encoded ? decodeURIComponent(encoded) : `${artifactId}.bin`,
  };
}

const EXTENSIONS: Record<ArtifactKind, string> = {
  markdown: "md",
  code: "txt",
  html: "html",
  svg: "svg",
  mermaid: "mmd",
  csv: "csv",
  json: "json",
  text: "txt",
  file: "bin",
};

const LANGUAGE_EXTENSIONS: Record<string, string> = {
  python: "py",
  javascript: "js",
  typescript: "ts",
  tsx: "tsx",
  jsx: "jsx",
  java: "java",
  go: "go",
  rust: "rs",
  c: "c",
  cpp: "cpp",
  csharp: "cs",
  ruby: "rb",
  php: "php",
  swift: "swift",
  kotlin: "kt",
  sql: "sql",
  bash: "sh",
  shell: "sh",
  yaml: "yaml",
  toml: "toml",
  hcl: "tf",
};

export function artifactFileName(artifact: {
  title: string;
  kind: ArtifactKind;
  language?: string | null;
}): string {
  const extension =
    (artifact.kind === "code" && artifact.language
      ? LANGUAGE_EXTENSIONS[artifact.language.toLowerCase()]
      : undefined) ?? EXTENSIONS[artifact.kind] ?? "txt";
  const base =
    artifact.title.trim().replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "") ||
    "artifact";
  return `${base}.${extension}`;
}

/** Query-state value identifying the artifact version shown in the panel. */
export function artifactParam(artifactId: string, version: number): string {
  return `${artifactId}:${version}`;
}

export function parseArtifactParam(
  value: string | null
): { artifactId: string; version: number } | null {
  if (!value) return null;
  const index = value.lastIndexOf(":");
  if (index <= 0) return null;
  const version = Number(value.slice(index + 1));
  if (!Number.isFinite(version) || version < 1) return null;
  return { artifactId: value.slice(0, index), version };
}
