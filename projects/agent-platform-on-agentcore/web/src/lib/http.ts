/**
 * The single fetch every authenticated API call goes through.
 *
 * It exists to make two failures recoverable that previously stranded the UI on
 * an error message with no way back:
 *
 * 1. **An expired access token.** The token lives 60 minutes and the
 *    AuthProvider's refresh timer only runs while the tab is awake, so a slept
 *    laptop or a late timer leaves storage claiming a live session while the
 *    backend 401s. A 401 now spends the refresh token once and replays the
 *    request; only if that fails does the session get cleared.
 *
 * 2. **A backend that is momentarily gone.** `uvicorn --reload` restarts on any
 *    watched file's mtime — even a `git checkout` that changes no content — and
 *    requests in that window never reach it, surfacing as "Failed to fetch".
 *    Those are retried with a short backoff instead of being reported as if the
 *    server were down for good.
 *
 * Callers keep throwing the same `Error` shapes as before, so existing error
 * handling is unaffected.
 */

import { authHeaders, refreshSession } from "@/lib/auth";

/** A request that never reached the server, as opposed to one it rejected. */
export class NetworkError extends Error {
  constructor(cause?: unknown) {
    super("서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

/**
 * How many times a request that never reached the server is retried, and how
 * long to wait between attempts. A backend reload takes a couple of seconds, so
 * three tries over ~1.5s covers it without making a genuinely-down server feel
 * like a hang.
 */
const NETWORK_RETRIES = 2;
const RETRY_DELAY_MS = 500;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Fetch with the session's Authorization header, refreshing once on 401 and
 * retrying transport failures. Returns the raw Response; status handling stays
 * with the caller, which owns its own error messages.
 */
export async function authedFetch(
  url: string,
  options: RequestInit = {},
  /** Set for FormData bodies, where the browser must supply the boundary. */
  omitContentType = false
): Promise<Response> {
  const buildHeaders = (): HeadersInit => ({
    ...(omitContentType ? {} : { "Content-Type": "application/json" }),
    ...authHeaders(),
    ...(options.headers || {}),
  });

  const attempt = () => fetch(url, { ...options, headers: buildHeaders() });

  let lastError: unknown;
  for (let i = 0; i <= NETWORK_RETRIES; i++) {
    try {
      const response = await attempt();

      // 401 means the token was rejected. Refresh once and replay: a second 401
      // after a fresh token is a real auth failure, not a stale one.
      if (response.status === 401 && i === 0) {
        const refreshed = await refreshSession();
        if (refreshed) return await attempt();
      }

      return response;
    } catch (err) {
      // fetch only rejects when the request never completed — DNS, connection
      // refused, aborted. A non-2xx response resolves, so it never lands here.
      lastError = err;
      if (i < NETWORK_RETRIES) await sleep(RETRY_DELAY_MS * (i + 1));
    }
  }

  throw new NetworkError(lastError);
}
