import assert from "node:assert/strict";
import { test } from "node:test";
import { modelLabel } from "./modelLabel.mjs";

test("Claude inference-profile ids become short family labels", () => {
  assert.equal(modelLabel("global.anthropic.claude-sonnet-5-5"), "Claude Sonnet 5.5");
  assert.equal(modelLabel("global.anthropic.claude-opus-5-5"), "Claude Opus 5.5");
  assert.equal(modelLabel("global.anthropic.claude-haiku-5-5"), "Claude Haiku 5.5");
  assert.equal(modelLabel("global.anthropic.claude-sonnet-5"), "Claude Sonnet 5");
  assert.equal(modelLabel("global.anthropic.claude-haiku-4-5-20251001-v1:0"), "Claude Haiku 4.5");
});

test("anything unrecognised is shown as-is, never mislabelled", () => {
  assert.equal(modelLabel("us.meta.llama4-maverick"), "us.meta.llama4-maverick");
  assert.equal(modelLabel("global.openai.gpt-5.6-sol"), "global.openai.gpt-5.6-sol");
  assert.equal(modelLabel(undefined), "");
  assert.equal(modelLabel(""), "");
});
