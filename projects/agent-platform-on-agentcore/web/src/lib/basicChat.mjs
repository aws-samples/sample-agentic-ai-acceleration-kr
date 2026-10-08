/**
 * Basic chat: the default runtime answering with an operator-curated model, no
 * registry agent. Plain `.mjs` so `node --test` can cover the decisions.
 *
 * The server owns the allow-list (GET /api/config → basicChat.models) and binds
 * the target itself; everything here only shapes what the picker offers and
 * which model a new chat asks for.
 */

/** Synthetic record id the server pins onto basic-chat threads. */
export const BASIC_CHAT_RECORD_ID = "__basic_chat__";
export const BASIC_CHAT_NAME = "기본 채팅";

/**
 * The agent to start a basic chat with, or null when the operator has not
 * configured one. A remembered model that has since left the allow-list falls
 * back to the first allowed one rather than sending a turn the server rejects.
 *
 * @param {{configured?: boolean, models?: string[]} | undefined} capability
 * @param {string | undefined} preferredModelId
 */
export function basicChatAgent(capability, preferredModelId) {
  const models = Array.isArray(capability?.models) ? capability.models : [];
  if (!capability?.configured || models.length === 0) return null;
  const modelId = models.includes(preferredModelId) ? preferredModelId : models[0];
  return {
    recordId: BASIC_CHAT_RECORD_ID,
    name: BASIC_CHAT_NAME,
    description: `${basicChatModelLabel(modelId)} · 에이전트 없이 모델과 바로 대화`,
    basicChat: true,
    basicChatModelId: modelId,
  };
}

/**
 * Short label for a Bedrock model id: "global.anthropic.claude-sonnet-5-5" →
 * "Claude Sonnet 5.5", "…claude-haiku-4-5-20251001-v1:0" → "Claude Haiku 4.5".
 * Anything unrecognised is shown as-is, so a new family is never mislabelled.
 */
export function basicChatModelLabel(modelId) {
  const match = String(modelId ?? "").match(
    /claude-(haiku|sonnet|opus)-(\d+)(?:-(\d))?(?:-\d{8})?(?:-v\d+(?::\d+)?)?$/i,
  );
  if (!match) return String(modelId ?? "");
  const [, family, major, minor] = match;
  const name = family[0].toUpperCase() + family.slice(1).toLowerCase();
  return `Claude ${name} ${major}${minor ? `.${minor}` : ""}`;
}
