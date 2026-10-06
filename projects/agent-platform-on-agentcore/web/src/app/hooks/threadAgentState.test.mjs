/**
 * A thread must not be sealed read-only by a failure that will pass.
 *
 * `useThreadAgent` decides whether a chat can be continued. Its fetcher wrapped
 * the registry lookup in a bare `catch` that returned `{problem:"unresolvable"}`
 * — a *resolved* value. SWR only retries rejections, so turning every failure
 * into a success switched retrying off entirely: one bad lookup pinned the
 * banner "이 대화의 에이전트를 찾을 수 없어 이어갈 수 없습니다" for the rest of
 * the session, even though the answer had streamed fine and was saved.
 *
 * The lookup fails for two unrelated reasons, and the server reports both as 502
 * (measured: `ResourceNotFoundException` for a deleted record and a transport
 * failure both surface that way):
 *
 *   - permanent  — the record was deleted, or this caller may no longer see it.
 *                  Read-only is the correct, final answer.
 *   - transient  — the server was busy or restarting. Read-only here is a lie,
 *                  and one that does not go away on its own.
 *
 * Distinguishing them is the whole fix, so it lives in a pure function that can
 * be tested without a DOM or a React renderer.
 *
 * Run: node --test src/app/hooks/threadAgentState.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  classifyLookupFailure,
  resolveThreadAgentState,
} from "./threadAgentState.mjs";

const AGENT = { recordId: "rec-1", name: "ks_text2sql_agent" };

// ── classifyLookupFailure ───────────────────────────────────────────────────

test("a deleted record is permanent", () => {
  // The API reports a missing record as 502 wrapping ResourceNotFoundException,
  // not as 404 — asserting the shape the server actually sends.
  assert.equal(
    classifyLookupFailure({
      status: 502,
      detail:
        "AWS error (ResourceNotFoundException): Registry record with ID aaaaaaaaaaaa not found.",
    }),
    "permanent"
  );
});

test("a malformed record id is permanent", () => {
  // A ValidationException cannot start succeeding: the id itself is wrong.
  assert.equal(
    classifyLookupFailure({
      status: 502,
      detail:
        "AWS error (ValidationException): 1 validation error detected: Value at 'recordId' failed to satisfy constraint",
    }),
    "permanent"
  );
});

test("a record this caller may not see is permanent", () => {
  assert.equal(
    classifyLookupFailure({ status: 403, detail: "Forbidden" }),
    "permanent"
  );
});

test("a busy or restarting server is transient", () => {
  // The case that produced the bug: nothing about the record is wrong.
  for (const status of [500, 502, 503, 504]) {
    assert.equal(
      classifyLookupFailure({ status, detail: "Internal Server Error" }),
      "transient",
      `status ${status} should be transient`
    );
  }
});

test("an unconfigured registry is transient", () => {
  // 503 from RegistryNotConfigured: an env fix away, not a dead record.
  assert.equal(
    classifyLookupFailure({ status: 503, detail: "Registry is not configured" }),
    "transient"
  );
});

test("a request that never reached the server is transient", () => {
  // authedFetch throws NetworkError with no status after exhausting its retries.
  assert.equal(classifyLookupFailure({ detail: "서버에 연결할 수 없습니다." }), "transient");
  assert.equal(classifyLookupFailure({}), "transient");
});

test("an expired session is transient", () => {
  // authedFetch already refreshes once on 401; a 401 still arriving means the
  // session is being re-established, which is not the record's fault.
  assert.equal(
    classifyLookupFailure({ status: 401, detail: "Not authenticated" }),
    "transient"
  );
});

test("an unknown failure defaults to transient", () => {
  // Fail toward recoverable: a wrong "transient" costs a retry, a wrong
  // "permanent" bricks the conversation for the session.
  assert.equal(classifyLookupFailure({ status: 418, detail: "???" }), "transient");
});

// ── resolveThreadAgentState ────────────────────────────────────────────────

test("no thread open follows the sidebar selection", () => {
  const state = resolveThreadAgentState({
    threadId: null,
    selected: AGENT,
    data: undefined,
    error: undefined,
    isLoading: false,
  });
  assert.deepEqual(state, { agent: AGENT, problem: null, loading: false });
});

test("a resolved pin yields its agent", () => {
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: undefined,
    data: { agent: AGENT },
    error: undefined,
    isLoading: false,
  });
  assert.deepEqual(state, { agent: AGENT, problem: null, loading: false });
});

test("the thread's pin wins over the sidebar selection", () => {
  // The server 409s a turn addressed to any other agent, so following the
  // selection would send a request that cannot succeed.
  const other = { recordId: "rec-2", name: "other_agent" };
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: other,
    data: { agent: AGENT },
    error: undefined,
    isLoading: false,
  });
  assert.equal(state.agent, AGENT);
});

test("a thread with no pin is unpinned, and stays that way", () => {
  // Written before pinning existed: which agent holds its history is unknowable,
  // so this really is final.
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: { problem: "unpinned" },
    error: undefined,
    isLoading: false,
  });
  assert.equal(state.problem, "unpinned");
  assert.equal(state.agent, null);
});

test("a deleted record is reported unresolvable", () => {
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: { problem: "unresolvable" },
    error: undefined,
    isLoading: false,
  });
  assert.equal(state.problem, "unresolvable");
  assert.equal(state.agent, null);
});

test("a transient failure does not report a problem", () => {
  // The regression under test. A rejected fetch is retried by SWR, so the state
  // it produces meanwhile must stay silent rather than declare the chat dead.
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: undefined,
    error: { status: 503, detail: "Internal Server Error" },
    isLoading: false,
  });
  assert.equal(
    state.problem,
    null,
    "a retryable failure must not seal the thread read-only"
  );
});

test("a transient failure keeps the chat in a loading state", () => {
  // Not "no agent and no problem" either — that renders an unexplained empty
  // state. While a retry is pending the honest state is "still resolving".
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: undefined,
    error: { status: 503, detail: "Internal Server Error" },
    isLoading: false,
  });
  assert.equal(state.loading, true);
  assert.equal(state.agent, null);
});

test("a permanent failure surfaced as an error is unresolvable", () => {
  // The fetcher rethrows so SWR can retry, so a permanent failure can also
  // arrive here as `error` rather than as data. It must still be final.
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: undefined,
    error: {
      status: 502,
      detail: "AWS error (ResourceNotFoundException): not found.",
    },
    isLoading: false,
  });
  assert.equal(state.problem, "unresolvable");
  assert.equal(state.loading, false, "a final answer is not still loading");
});

test("opening a thread does not flash the empty state", () => {
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: undefined,
    error: undefined,
    isLoading: true,
  });
  assert.equal(state.loading, true);
  assert.equal(state.problem, null);
});

test("nothing known yet still reads as loading", () => {
  // isLoading is false between SWR retries, so it cannot be the only signal.
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: AGENT,
    data: undefined,
    error: undefined,
    isLoading: false,
  });
  assert.equal(state.loading, true);
});

test("data wins once it arrives, even alongside a stale error", () => {
  // A successful retry leaves SWR's previous error in place; the fresh data is
  // the answer, or a recovered lookup would still show the banner.
  const state = resolveThreadAgentState({
    threadId: "t-1",
    selected: undefined,
    data: { agent: AGENT },
    error: { status: 503, detail: "Internal Server Error" },
    isLoading: false,
  });
  assert.equal(state.agent, AGENT);
  assert.equal(state.problem, null);
  assert.equal(state.loading, false);
});
