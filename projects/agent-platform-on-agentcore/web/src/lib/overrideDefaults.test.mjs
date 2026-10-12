import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeOverrideDraft } from "./overrideDefaults.mjs";

const defaults = {
  model_id: "global.anthropic.claude-sonnet-5-5",
  system_prompt: "You are a careful analyst.\n",
};

test("a draft equal to the agent's defaults stores no override", () => {
  // The popover pre-fills the agent's own prompt; applying it untouched must not
  // freeze that text onto the thread (a redeploy would then be silently ignored).
  assert.deepEqual(
    normalizeOverrideDraft(
      { modelId: "global.anthropic.claude-sonnet-5-5", systemPrompt: "You are a careful analyst." },
      defaults
    ),
    {}
  );
});

test("a different model or prompt is kept, trimmed", () => {
  assert.deepEqual(
    normalizeOverrideDraft(
      { modelId: "global.anthropic.claude-opus-5-5", systemPrompt: "  Answer in Korean.  " },
      defaults
    ),
    { modelId: "global.anthropic.claude-opus-5-5", systemPrompt: "Answer in Korean." }
  );
});

test("empty fields store nothing even when the defaults are unknown", () => {
  assert.deepEqual(normalizeOverrideDraft({ modelId: "", systemPrompt: "   " }, null), {});
  assert.deepEqual(
    normalizeOverrideDraft({ modelId: "", systemPrompt: "Be brief." }, null),
    { systemPrompt: "Be brief." }
  );
});
