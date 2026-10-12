/**
 * Writing a `toolResult` event onto the conversation — the rule, separated from
 * the hook.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM or a React
 * renderer, following settleToolCalls.mjs. See applyToolResult.test.mjs for the
 * failures this exists to prevent.
 */

/**
 * The same conversation with `event`'s result attached to the call it belongs to.
 *
 * The result is **assigned, not appended**. A harness streams one tool result
 * across many events and the server accumulates them, so each event already
 * carries the whole result so far — appending here would repeat every prefix.
 * That contract is why `status` has to be re-read on every event too: the server
 * restamps it precisely because this function overwrites.
 *
 * `status` on the event is the tool's own verdict in the API's vocabulary
 * ("success" | "error"), which is not the UI's — a tool call is rendered from
 * "pending" | "completed" | "error" | "interrupted", where the first and last are
 * lifecycle states the wire has no business naming. So only "error" is carried
 * across; anything else, including a stream that reported no status at all, means
 * the call is simply done. Mapping rather than passing through also keeps an
 * unrecognised future enum value from reaching the renderer as an unknown status,
 * which draws no icon.
 *
 * Only the first matching call is updated, and search runs oldest message first:
 * ids are unique, so a second match would be the same call seen twice.
 *
 * Returns the original array when no call matches — a result for an unknown id is
 * nothing this can act on — so React can skip the re-render.
 *
 * @template M
 * @param {M[] | undefined} messages
 * @param {{toolUseId?: string, result?: unknown, status?: string}} event
 * @returns {M[]}
 */
export function applyToolResult(messages, event) {
  if (!Array.isArray(messages)) return [];

  const toolUseId = event?.toolUseId;
  if (!toolUseId) return messages;

  const status = event?.status === "error" ? "error" : "completed";

  for (let i = 0; i < messages.length; i++) {
    const message = messages[i];
    if (message?.type !== "ai") continue;

    const toolCalls = message.tool_calls;
    if (!Array.isArray(toolCalls)) continue;

    const index = toolCalls.findIndex((toolCall) => toolCall?.id === toolUseId);
    if (index < 0) continue;

    const next = [...messages];
    next[i] = {
      ...message,
      tool_calls: toolCalls.map((toolCall, j) =>
        j === index ? { ...toolCall, result: event.result, status } : toolCall
      ),
    };
    return next;
  }

  return messages;
}
