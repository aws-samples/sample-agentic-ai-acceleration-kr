/**
 * A file the agent produced must reach the screen.
 *
 * The harness sweep stored every file with `message_id=None` — measured on the
 * deployed stack, six rows out of six — because the stream clears its message
 * tracking at `messageStop` and the sweep runs after it. The chat only renders
 * cards inside a message, matching on `messageId` or `toolCallId`, so those rows
 * were dropped by every message: stored, previewable, downloadable through the
 * API, and absent from the chat.
 *
 * Run: node --test src/app/components/messageArtifacts.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { assignArtifactsToMessages } from "./messageArtifacts.mjs";

const THREAD = [
  { id: "u-1", type: "human" },
  { id: "msg-1", type: "ai", toolCallIds: ["tool-a"] },
  { id: "u-2", type: "human" },
  { id: "msg-2", type: "ai", toolCallIds: ["tool-b"] },
];

const file = (extra) => ({ artifactId: "a1", version: 1, kind: "file", ...extra });

test("an artifact naming its message lands on it", () => {
  const buckets = assignArtifactsToMessages(THREAD, [file({ messageId: "msg-1" })]);
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0, 1, 0, 0]
  );
});

test("an artifact naming a tool call lands on the message that made it", () => {
  const buckets = assignArtifactsToMessages(THREAD, [file({ toolCallId: "tool-b" })]);
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0, 0, 0, 1]
  );
});

test("a swept file with no message id lands on the last AI message", () => {
  // The regression: this used to match nothing and render nowhere.
  const buckets = assignArtifactsToMessages(THREAD, [
    file({ filename: "최종본.docx" }),
  ]);
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0, 0, 0, 1]
  );
  assert.equal(buckets[3][0].filename, "최종본.docx");
});

test("an untyped message still counts as an AI turn", () => {
  // A message rebuilt from storage has been seen without a `type`, and it renders
  // as an assistant turn — so it must be able to hold a card.
  const buckets = assignArtifactsToMessages(
    [{ id: "u-1", type: "human" }, { id: "msg-1" }],
    [file({})]
  );
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0, 1]
  );
});

test("an artifact naming a message absent from the thread is not relabelled", () => {
  // A stale row must not be dragged onto the latest turn: it would claim the
  // current answer produced a file it never made.
  const buckets = assignArtifactsToMessages(THREAD, [
    file({ messageId: "msg-from-another-thread" }),
  ]);
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0, 0, 0, 0]
  );
});

test("nothing is placed when the thread has no AI message", () => {
  // Cards render only for `!isUser`, so a human-only thread has nowhere to put
  // one. Placing it on the human message would silently drop it anyway.
  const buckets = assignArtifactsToMessages([{ id: "u-1", type: "human" }], [file({})]);
  assert.deepEqual(
    buckets.map((b) => b.length),
    [0]
  );
});

test("every message gets a bucket even with no artifacts", () => {
  assert.deepEqual(assignArtifactsToMessages(THREAD, []), [[], [], [], []]);
  assert.deepEqual(assignArtifactsToMessages(THREAD, undefined), [[], [], [], []]);
});
