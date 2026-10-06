/**
 * A tool that failed must not be drawn as a tool that worked.
 *
 * `ToolCallBox` picks its icon from `status`: "completed" draws a check,
 * "error" draws a red alert. The status used to be set from the mere arrival of
 * a result, so a tool that raised and returned a stack trace still got the
 * check — and the only way to find out the agent had been working from an error
 * was to expand the box and read it. The harness does report the verdict
 * (`HarnessToolResultBlockStart.status`), the server now forwards it, and this
 * is where it lands.
 *
 * The other half is that the result is assigned rather than appended: the server
 * accumulates a chunked result and re-sends the running total on every event, so
 * appending here would repeat every prefix. Both halves depend on the same
 * overwrite contract, which is why they are tested together.
 *
 * Run: node --test src/hooks/applyToolResult.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyToolResult } from "./applyToolResult.mjs";

const pending = (id = "t1", name = "calculate") => ({
  id,
  name,
  args: "{}",
  status: "pending",
});

const conversation = (...toolCalls) => [
  { id: "u1", type: "human", content: "2**20?" },
  { id: "m1", type: "ai", content: "계산하겠습니다.", tool_calls: toolCalls },
];

const callIn = (messages, index = 0) => messages[1].tool_calls[index];

test("a result settles the call it belongs to", () => {
  const applied = applyToolResult(conversation(pending()), {
    toolUseId: "t1",
    result: "1048576",
  });

  assert.equal(callIn(applied).result, "1048576");
  assert.equal(callIn(applied).status, "completed");
});

test("a reported failure is rendered as a failure", () => {
  const applied = applyToolResult(conversation(pending()), {
    toolUseId: "t1",
    result: "ZeroDivisionError: division by zero",
    status: "error",
  });

  assert.equal(callIn(applied).status, "error");
  assert.equal(callIn(applied).result, "ZeroDivisionError: division by zero");
});

test("a reported success is the UI's completed", () => {
  // The wire says "success"; the renderer knows no such status and would draw
  // no icon at all for it.
  const applied = applyToolResult(conversation(pending()), {
    toolUseId: "t1",
    result: "1048576",
    status: "success",
  });

  assert.equal(callIn(applied).status, "completed");
});

test("an unrecognised status does not become an unknown status", () => {
  // Anything the renderer cannot draw has to collapse to "completed": the result
  // did arrive, so the call is not running any more either way.
  const applied = applyToolResult(conversation(pending()), {
    toolUseId: "t1",
    result: "중단",
    status: "cancelled",
  });

  assert.equal(callIn(applied).status, "completed");
});

test("a later event replaces the result rather than appending to it", () => {
  // The server sends the running total, so appending would give "abababc".
  let messages = conversation(pending());
  for (const result of ["ab", "abab", "ababc"]) {
    messages = applyToolResult(messages, { toolUseId: "t1", result });
  }

  assert.equal(callIn(messages).result, "ababc");
});

test("a failure is not undone by the events that follow it", () => {
  // status rides every event precisely so this holds; if the server ever stops
  // restamping it, this is the test that says what breaks.
  let messages = applyToolResult(conversation(pending()), {
    toolUseId: "t1",
    result: "Traceback",
    status: "error",
  });
  messages = applyToolResult(messages, {
    toolUseId: "t1",
    result: "Traceback (most recent call last)",
    status: "error",
  });

  assert.equal(callIn(messages).status, "error");
});

test("only the call named by the event is touched", () => {
  const applied = applyToolResult(conversation(pending("t1"), pending("t2")), {
    toolUseId: "t2",
    result: "done",
    status: "error",
  });

  assert.equal(callIn(applied, 0).status, "pending");
  assert.equal(callIn(applied, 1).status, "error");
});

test("a result for an unknown call leaves the conversation alone", () => {
  // Referential identity, so React skips the re-render.
  const messages = conversation(pending("t1"));

  assert.equal(applyToolResult(messages, { toolUseId: "nope", result: "x" }), messages);
});

test("an event with no toolUseId is ignored", () => {
  const messages = conversation(pending());

  assert.equal(applyToolResult(messages, { result: "x" }), messages);
});

test("a human message carrying tool_calls is not searched", () => {
  // Only assistant messages own tool calls; matching elsewhere would attach the
  // result to a bubble that never renders one.
  const messages = [
    { id: "u1", type: "human", content: "q", tool_calls: [pending("t1")] },
  ];

  assert.equal(applyToolResult(messages, { toolUseId: "t1", result: "x" }), messages);
});

test("the original conversation is never mutated", () => {
  // The hook holds these objects in React state; rewriting one in place would
  // change a rendered message without a re-render.
  const call = pending();
  const messages = conversation(call);

  applyToolResult(messages, { toolUseId: "t1", result: "1048576", status: "error" });

  assert.equal(call.status, "pending");
  assert.equal(call.result, undefined);
});

test("an empty conversation is handled", () => {
  assert.deepEqual(applyToolResult([], { toolUseId: "t1", result: "x" }), []);
  assert.deepEqual(applyToolResult(undefined, { toolUseId: "t1", result: "x" }), []);
});
