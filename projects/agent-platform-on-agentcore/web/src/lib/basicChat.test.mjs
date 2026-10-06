import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BASIC_CHAT_RECORD_ID,
  basicChatAgent,
  basicChatModelLabel,
} from "./basicChat.mjs";

const caps = {
  configured: true,
  models: ["global.anthropic.claude-sonnet-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0"],
};

test("no agent when the operator has not configured basic chat", () => {
  assert.equal(basicChatAgent(undefined, undefined), null);
  assert.equal(basicChatAgent({ configured: false, models: [] }, undefined), null);
  assert.equal(basicChatAgent({ configured: true, models: [] }, undefined), null);
});

test("first allowed model by default, remembered model when still allowed", () => {
  assert.equal(basicChatAgent(caps, undefined).basicChatModelId, caps.models[0]);
  assert.equal(basicChatAgent(caps, caps.models[1]).basicChatModelId, caps.models[1]);
});

test("a remembered model that left the allow-list falls back instead of failing", () => {
  const agent = basicChatAgent(caps, "global.anthropic.claude-opus-4-8");
  assert.equal(agent.basicChatModelId, caps.models[0]);
  assert.equal(agent.recordId, BASIC_CHAT_RECORD_ID);
  assert.equal(agent.basicChat, true);
});

test("model labels", () => {
  assert.equal(basicChatModelLabel("global.anthropic.claude-sonnet-5"), "Claude Sonnet 5");
  assert.equal(basicChatModelLabel("global.anthropic.claude-opus-5-5"), "Claude Opus 5.5");
  assert.equal(
    basicChatModelLabel("global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "Claude Haiku 4.5",
  );
  assert.equal(basicChatModelLabel("us.meta.llama4-maverick"), "us.meta.llama4-maverick");
  assert.equal(basicChatModelLabel(undefined), "");
});
