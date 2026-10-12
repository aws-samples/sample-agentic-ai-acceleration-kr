/**
 * Whether a chat can be continued — the decisions, separated from the hook.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM or a React
 * renderer: what matters here is the classification and the state machine, and
 * both are pure. `useThreadAgent` supplies SWR's data/error and renders the
 * result. See threadAgentState.test.mjs for the failure this exists to prevent.
 */

/**
 * Failures that cannot start succeeding, whatever we do.
 *
 * The API reports a deleted record as 502 wrapping an AWS exception rather than
 * as 404 (measured), so the status alone cannot decide — the AWS error name
 * inside the detail is the actual signal.
 */
const PERMANENT_AWS_ERRORS = [
  // The record is gone.
  "ResourceNotFoundException",
  // The id cannot name a record: wrong shape, so no retry will find it.
  "ValidationException",
];

/** Statuses that are about *this caller's* right to see the record, not timing. */
const PERMANENT_STATUSES = new Set([403, 404]);

/**
 * Split a failed registry lookup into "final" and "worth retrying".
 *
 * Everything unrecognised is transient on purpose. The two mistakes are not
 * symmetrical: calling a permanent failure transient costs a few retries that
 * settle into the same read-only state, while calling a transient failure
 * permanent seals a live conversation read-only for the rest of the session —
 * which is the bug this module was written for.
 *
 * @param {{status?: number, detail?: string}} failure
 * @returns {"permanent" | "transient"}
 */
export function classifyLookupFailure(failure) {
  const { status, detail = "" } = failure || {};

  if (PERMANENT_AWS_ERRORS.some((name) => detail.includes(name))) {
    return "permanent";
  }
  if (status !== undefined && PERMANENT_STATUSES.has(status)) {
    return "permanent";
  }
  // A 401 that survived authedFetch's one refresh included: the session is being
  // re-established, which says nothing about the record.
  return "transient";
}

/**
 * Fold SWR's state into what the chat should do.
 *
 * @param {{
 *   threadId: string | null,
 *   selected: object | undefined,
 *   data: {agent?: object, problem?: string} | undefined,
 *   error: {status?: number, detail?: string} | undefined,
 *   isLoading: boolean,
 * }} input
 * @returns {{agent: object | null, problem: string | null, loading: boolean}}
 */
export function resolveThreadAgentState({
  threadId,
  selected,
  data,
  error,
  isLoading,
}) {
  // No thread open: the sidebar selection is correct, because that is what a new
  // chat starts.
  if (!threadId) {
    return { agent: selected ?? null, problem: null, loading: false };
  }

  // Data wins over a leftover error. A successful retry leaves SWR's previous
  // error in place, and reading that would keep the banner up on a lookup that
  // has already recovered.
  if (data) {
    return {
      agent: data.agent ?? null,
      problem: data.problem ?? null,
      loading: false,
    };
  }

  if (error) {
    // Only a permanent failure is an answer. A transient one reads as "still
    // resolving": SWR is retrying it, and claiming the agent is missing in the
    // meantime is both wrong and — since nothing re-renders it away — sticky.
    if (classifyLookupFailure(error) === "permanent") {
      return { agent: null, problem: "unresolvable", loading: false };
    }
    return { agent: null, problem: null, loading: true };
  }

  // Nothing known yet. `isLoading` is false between SWR's retries, so it cannot
  // be the only signal — without this the chat would flash its empty state in
  // every backoff window.
  return { agent: null, problem: null, loading: true };
}
