/**
 * A spinner must not outlive the stream that opened it.
 *
 * The UI marks a tool call "pending" on its `contentBlockStart` and settles it on
 * a matching `toolResult` — or, failing that, when `messageStop` closes the
 * message. That fallback is the only one there is, so it covers exactly the
 * endings that manage to send a `messageStop`.
 *
 * A dropped connection sends nothing. The `fetch` promise rejects (or the reader
 * simply reaches `done`) with the message half-written, no `messageStop` and no
 * `toolResult`, and the call stays pending forever: `isLoading` goes false, the
 * send button comes back, and a tool box goes on spinning until the page is
 * reloaded. The server now settles its own side on every ending, but it cannot
 * help here — if the connection is gone, its frames do not arrive at all.
 *
 * Measured against the deployed ks_text2sql_agent: `sql_specialist` blocks stay
 * legitimately pending for 13-102s, because a parallel call waits for the earlier
 * one. So "still spinning" is normal for a minute or more and cannot be timed out;
 * the end of the stream is the only honest signal that no result is coming.
 *
 * Run: node --test src/hooks/settleToolCalls.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { settlePendingToolCalls } from "./settleToolCalls.mjs";

const pending = (id, name) => ({ id, name, args: "{}", status: "pending" });

test("a call left pending when the stream ends is settled", () => {
  const messages = [
    { id: "m1", type: "ai", content: "조회하겠습니다.", tool_calls: [pending("t1", "ask_sql_specialist")] },
  ];

  const settled = settlePendingToolCalls(messages);

  assert.equal(settled[0].tool_calls[0].status, "completed");
});

test("settling invents no result the tool never reported", () => {
  const messages = [
    { id: "m1", type: "ai", content: "", tool_calls: [pending("t1", "ask_sql_specialist")] },
  ];

  const settled = settlePendingToolCalls(messages);

  assert.equal(settled[0].tool_calls[0].result, undefined);
});

test("a reported result is left exactly as it was", () => {
  const messages = [
    {
      id: "m1",
      type: "ai",
      content: "",
      tool_calls: [{ id: "t1", name: "ask_sql_specialist", status: "completed", result: "5 rows" }],
    },
  ];

  const settled = settlePendingToolCalls(messages);

  assert.equal(settled[0].tool_calls[0].result, "5 rows");
  assert.equal(settled[0].tool_calls[0].status, "completed");
});

test("every pending call across every message is settled", () => {
  const messages = [
    { id: "m1", type: "ai", content: "", tool_calls: [pending("t1", "a"), pending("t2", "b")] },
    { id: "m2", type: "ai", content: "", tool_calls: [pending("t3", "c")] },
  ];

  const settled = settlePendingToolCalls(messages);

  const statuses = settled.flatMap((m) => m.tool_calls.map((t) => t.status));
  assert.deepEqual(statuses, ["completed", "completed", "completed"]);
});

test("messages without tool calls are untouched", () => {
  const messages = [
    { id: "h1", type: "human", content: "질문" },
    { id: "m1", type: "ai", content: "답변" },
  ];

  const settled = settlePendingToolCalls(messages);

  assert.deepEqual(settled, messages);
});

test("nothing pending means the same array back", () => {
  // Referential identity, so React re-renders only when something actually
  // changed: this runs on every stream end, including the overwhelming majority
  // that ended cleanly.
  const messages = [
    { id: "m1", type: "ai", content: "", tool_calls: [{ id: "t1", name: "a", status: "completed" }] },
  ];

  assert.equal(settlePendingToolCalls(messages), messages);
});

test("an interrupted call is not overwritten as completed", () => {
  // "interrupted" is a settled state that already says what happened; relabelling
  // it "completed" would claim the call finished normally.
  const messages = [
    { id: "m1", type: "ai", content: "", tool_calls: [{ id: "t1", name: "a", status: "interrupted" }] },
  ];

  const settled = settlePendingToolCalls(messages);

  assert.equal(settled[0].tool_calls[0].status, "interrupted");
});

test("an empty conversation is handled", () => {
  assert.deepEqual(settlePendingToolCalls([]), []);
  assert.deepEqual(settlePendingToolCalls(undefined), []);
});

test("the original conversation is never mutated", () => {
  // The hook holds these objects in React state; rewriting one in place would
  // change a rendered message without a re-render.
  const call = pending("t1", "ask_sql_specialist");
  const messages = [{ id: "m1", type: "ai", content: "", tool_calls: [call] }];

  settlePendingToolCalls(messages);

  assert.equal(call.status, "pending");
});
