/**
 * Streamed tool arguments must become an object when their block closes.
 *
 * The card app (`show_content`) renders from the tool's *input*. Live, the model
 * wrote the card and the server stored a complete `{title, markdown}` — yet the
 * card stayed empty and the host re-called the tool with `{}`. The arguments had
 * never been parsed: the hook's `contentBlockStop` handler looked the call up by a
 * mutable `currentToolUseId` inside a deferred `setState` updater, and had already
 * reset that variable to null by the time React ran it. Whenever the stop arrived
 * in the same chunk as a burst of deltas (the usual case — the proxy delivers the
 * block in bursts), the parse silently did not happen, and the `messageStop`
 * settle that followed only flipped the status.
 *
 * This helper takes the id as a parameter so the caller has to snapshot it, and
 * these tests pin down what the close must do to the call.
 *
 * Run: node --test src/hooks/closeToolUse.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { closeToolUse } from "./closeToolUse.mjs";

const streaming = (id, args) => ({ id, name: "platform-status___show_content", args, status: "pending" });

const conversation = (...toolCalls) => [
  { id: "u1", type: "human", content: "카드로 보여줘" },
  { id: "m1", type: "ai", content: "", tool_calls: toolCalls },
];

const callIn = (messages, index = 0) => messages[1].tool_calls[index];

test("the streamed JSON text becomes an object and the call is completed", () => {
  const text = '{"markdown": "| a | b |\\n|---|---|", "title": "비교"}';
  const closed = closeToolUse(conversation(streaming("t1", text)), "m1", "t1");
  const call = callIn(closed);
  assert.equal(call.status, "completed");
  assert.deepEqual(call.args, { markdown: "| a | b |\n|---|---|", title: "비교" });
});

test("only the named call is closed; a sibling still streaming is untouched", () => {
  const closed = closeToolUse(
    conversation(streaming("t1", '{"title": "A", "markdown": "x"}'), streaming("t2", '{"title": "B"')),
    "m1",
    "t1",
  );
  assert.equal(callIn(closed, 0).status, "completed");
  assert.deepEqual(callIn(closed, 0).args, { title: "A", markdown: "x" });
  assert.equal(callIn(closed, 1).status, "pending");
  assert.equal(callIn(closed, 1).args, '{"title": "B"');
});

test("without an id there is nothing to close — the very case the hook used to hit", () => {
  const messages = conversation(streaming("t1", '{"title": "A"}'));
  assert.equal(closeToolUse(messages, "m1", null), messages);
  assert.equal(closeToolUse(messages, "m1", undefined), messages);
  assert.equal(callIn(messages).status, "pending");
});

test("an unknown call or message leaves the conversation as it was", () => {
  const messages = conversation(streaming("t1", "{}"));
  assert.equal(closeToolUse(messages, "m1", "t9"), messages);
  assert.equal(closeToolUse(messages, "m9", "t1"), messages);
});

test("text that is not JSON is kept verbatim but the call still completes", () => {
  const closed = closeToolUse(conversation(streaming("t1", '{"title": "unterminated')), "m1", "t1");
  assert.equal(callIn(closed).status, "completed");
  assert.equal(callIn(closed).args, '{"title": "unterminated');
});

test("arguments already an object are passed through unchanged", () => {
  const closed = closeToolUse(conversation(streaming("t1", { title: "A" })), "m1", "t1");
  assert.deepEqual(callIn(closed).args, { title: "A" });
  assert.equal(callIn(closed).status, "completed");
});
