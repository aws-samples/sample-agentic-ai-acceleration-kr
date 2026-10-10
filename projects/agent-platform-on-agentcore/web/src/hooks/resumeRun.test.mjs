/**
 * What a reopened busy thread keeps from the stored record.
 *
 * The run saves its partial turn on every messageStop, so the stored record
 * already holds some of the AI and tool messages the replay is about to send
 * again, under the same ids. Keeping them would double their text as the
 * replayed deltas append. The server tells the client exactly which messages
 * predate the run (plus the run's own question) in a `run_baseline` frame;
 * this applies it. Guessing from "the last human message" was wrong for runs
 * started without a new human message and when the row was read before the
 * question was saved.
 *
 * Run: node --test src/hooks/resumeRun.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyRunBaseline } from "./resumeRun.mjs";

const human = (id) => ({ id, type: "human", content: "q" });
const ai = (id) => ({ id, type: "ai", content: "partial" });
const tool = (id) => ({ id, type: "tool", content: "r" });

test("keeps exactly the baseline messages, in stored order", () => {
  const out = applyRunBaseline(
    [human("h1"), ai("a1"), human("h2"), ai("a2"), tool("t2")],
    ["h1", "a1", "h2"]
  );
  assert.deepEqual(out.map((m) => m.id), ["h1", "a1", "h2"]);
});

test("keeps a finished earlier answer when the run started without a new question", () => {
  const input = [human("h1"), ai("a1"), ai("a2-partial")];
  assert.deepEqual(applyRunBaseline(input, ["h1", "a1"]).map((m) => m.id), ["h1", "a1"]);
});

test("a message without an id cannot be the run's and is kept", () => {
  const legacy = { type: "ai", content: "old" };
  assert.deepEqual(applyRunBaseline([legacy, human("h1"), ai("a1")], ["h1"]), [legacy, human("h1")]);
});

test("tolerates an empty or missing list and baseline", () => {
  assert.deepEqual(applyRunBaseline([], ["x"]), []);
  assert.deepEqual(applyRunBaseline(undefined, ["x"]), []);
  assert.deepEqual(applyRunBaseline([human("h1")], undefined), [human("h1")]);
});
