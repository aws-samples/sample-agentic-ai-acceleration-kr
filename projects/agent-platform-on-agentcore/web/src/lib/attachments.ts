/**
 * Attachment upload and lookup.
 *
 * The format list and size ceiling mirror the server's, which is the real
 * enforcement point — these exist so the file picker filters sensibly and an
 * obvious mistake is caught before a round trip.
 */
import { authedFetch } from "@/lib/http";

// Each lib module declares its own API_BASE (see registry.ts:121,
// artifacts.ts:72) — there is no shared export.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export class AttachmentApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "AttachmentApiError";
  }
}

/** `fail` in knowledge.ts is module-private, so this mirrors it. */
async function fail(response: Response): Promise<never> {
  const body = await response
    .json()
    .catch(() => ({ detail: response.statusText }));
  throw new AttachmentApiError(
    response.status,
    body.detail || `HTTP ${response.status}`
  );
}

export type AttachmentKind = "image" | "document";

/** What a message's content array carries. Small on purpose: no bytes. */
export type AttachmentRef = {
  type: "attachment";
  attachment_id: string;
  filename: string;
  media_type: string;
  kind: AttachmentKind;
};

/** Composer-local state for one file, from selection to uploaded. */
export type PendingAttachment = {
  localId: string;
  file: File;
  state: "uploading" | "ready" | "error";
  error?: string;
  ref?: AttachmentRef;
  /** Object URL for an image thumbnail while it uploads. */
  previewUrl?: string;
};

const IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "gif", "webp"];
const DOCUMENT_EXTENSIONS = [
  "pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "htm", "txt", "md",
];

export const MAX_BYTES = 4_718_592; // 4.5 MB, same as the server
export const MAX_FILES = 5;

export const ACCEPT_ATTRIBUTE = [...IMAGE_EXTENSIONS, ...DOCUMENT_EXTENSIONS]
  .map((extension) => `.${extension}`)
  .join(",");

function extensionOf(name: string): string {
  const index = name.lastIndexOf(".");
  return index === -1 ? "" : name.slice(index + 1).toLowerCase();
}

export function isAllowed(file: File): boolean {
  const extension = extensionOf(file.name);
  return (
    IMAGE_EXTENSIONS.includes(extension) ||
    DOCUMENT_EXTENSIONS.includes(extension)
  );
}

export function isImage(filename: string): boolean {
  return IMAGE_EXTENSIONS.includes(extensionOf(filename));
}

/** Multipart, so no Content-Type: the browser must supply its own boundary. */
export async function uploadAttachment(
  threadId: string,
  file: File
): Promise<AttachmentRef> {
  const form = new FormData();
  form.append("file", file);

  const response = await authedFetch(
    `${API_BASE}/threads/${encodeURIComponent(threadId)}/attachments`,
    { method: "POST", body: form },
    true
  );
  if (!response.ok) await fail(response);

  const stored = await response.json();
  return {
    type: "attachment",
    attachment_id: stored.attachment_id,
    filename: stored.filename,
    media_type: stored.media_type,
    kind: stored.kind,
  };
}

export function attachmentUrl(threadId: string, attachmentId: string): string {
  return `${API_BASE}/threads/${encodeURIComponent(
    threadId
  )}/attachments/${encodeURIComponent(attachmentId)}`;
}

/**
 * The attachment's bytes as an object URL an `<img>` or a download link can use,
 * or null when it cannot be read.
 *
 * Not `attachmentUrl` straight into `<img src>` / `<a href>`, which is what the
 * message strip did first and which only appears to work: the route is gated on
 * `current_user`, and an `<img>` or a plain link sends no Authorization header,
 * so with AUTH_ENFORCED on (the default) every thumbnail 401s and every link
 * opens an error page — found in review, and the same flaw browserScreenshots.ts
 * had already run into. The bytes are fetched with the session's token instead
 * and handed to the element as an object URL.
 *
 * Null rather than throwing: a missing attachment, a server without the bucket
 * configured, and a dropped connection all leave the strip showing the filename
 * without a preview, which is the right answer for each.
 *
 * The caller owns the returned URL and must `URL.revokeObjectURL` it.
 */
export async function fetchAttachment(
  threadId: string,
  attachmentId: string
): Promise<string | null> {
  try {
    const response = await authedFetch(attachmentUrl(threadId, attachmentId));
    if (!response.ok) return null;
    return URL.createObjectURL(await response.blob());
  } catch {
    return null;
  }
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
