/**
 * Turn the override popover's draft into what gets stored on the thread.
 *
 * The popover pre-fills the agent's own model and system prompt so the user
 * can see what they are replacing. Storing those same values back as an
 * override would be a trap: the thread would answer with a copy of today's
 * prompt even after the agent is redeployed with a new one, and the "override
 * in force" dot would light for a thread that changes nothing. So anything
 * equal to the default — or empty — is dropped, and only a real difference
 * is kept. Plain JS (not TS) so it can be unit-tested with `node --test`.
 *
 * @param {{modelId?: string, systemPrompt?: string}} draft
 * @param {{model_id?: string|null, system_prompt?: string|null} | null | undefined} defaults
 * @returns {{modelId?: string, systemPrompt?: string}}
 */
export function normalizeOverrideDraft(draft, defaults) {
  const out = {};
  const modelId = (draft.modelId ?? "").trim();
  if (modelId && modelId !== (defaults?.model_id ?? "")) out.modelId = modelId;
  const systemPrompt = (draft.systemPrompt ?? "").trim();
  if (systemPrompt && systemPrompt !== (defaults?.system_prompt ?? "").trim()) {
    out.systemPrompt = systemPrompt;
  }
  return out;
}
