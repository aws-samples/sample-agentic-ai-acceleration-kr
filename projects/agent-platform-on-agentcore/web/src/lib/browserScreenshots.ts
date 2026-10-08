/**
 * Fetching a browser screenshot the platform stored.
 *
 * The recognition rule lives in hooks/browserScreenshot.mjs, which is pure and
 * tested; this module is how the bytes are actually obtained.
 *
 * Not by putting the route in an `<img src>`, which is the obvious thing and does
 * not work: the route is gated on `current_user`, and a plain `<img>` cannot carry
 * an Authorization header, so the browser gets 401 and fires `onerror`. Verified
 * against the deployed stack — locally it appears to work only because
 * AUTH_ENFORCED is false there, which is exactly the kind of difference a local
 * check hides. So the image is fetched through `authedFetch` and handed to the
 * `<img>` as an object URL instead. The attachment thumbnails in
 * MessageAttachment.tsx go through fetchAttachment for the same reason.
 */

import { authedFetch } from "@/lib/http";

// Each lib module declares its own API_BASE — see attachments.ts:12.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * Where to re-fetch the PNG one tool call captured.
 *
 * Addressed by tool call, never by S3 key: the server looks the key up in the
 * thread's own stored messages, so owning the thread is all the caller has to
 * prove and no key from this side is trusted. The flip side is that it only
 * answers once the turn has been persisted — mid-stream the presigned URL in the
 * tool result is the one that works. Both stop working after 7 days, when the
 * bucket expires the object.
 */
export function browserScreenshotUrl(
  threadId: string,
  toolCallId: string
): string {
  return `${API_BASE}/threads/${encodeURIComponent(
    threadId
  )}/browser-screenshots/${encodeURIComponent(toolCallId)}`;
}

/**
 * The stored screenshot as an object URL an `<img>` can display, or null.
 *
 * Null rather than throwing: a screenshot that has gone (the bucket expires them
 * after 7 days) or a server without the bucket configured are both ordinary, and
 * the caller's answer to all of them is the same — leave the image out and let the
 * result JSON speak for itself.
 *
 * The caller owns the returned URL and must `URL.revokeObjectURL` it, or every
 * re-render of a long thread leaks a screenshot's worth of memory.
 */
export async function fetchBrowserScreenshot(
  threadId: string,
  toolCallId: string
): Promise<string | null> {
  try {
    const response = await authedFetch(
      browserScreenshotUrl(threadId, toolCallId)
    );
    if (!response.ok) return null;
    return URL.createObjectURL(await response.blob());
  } catch {
    return null;
  }
}
