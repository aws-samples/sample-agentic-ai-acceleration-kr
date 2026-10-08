/**
 * Closing a tool-use block when its `contentBlockStop` arrives — the rule,
 * separated from the hook.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM or a React
 * renderer, following applyToolResult.mjs. See closeToolUse.test.mjs for the
 * failure this exists to prevent.
 */

/**
 * The same conversation with one tool call's streamed arguments finalised.
 *
 * While a tool-use block streams, its `args` is the raw JSON text accumulated
 * from `contentBlockDelta` events. The block's `contentBlockStop` is the one
 * moment the text is known to be complete, so this is where it becomes an object
 * and the call stops being "pending". Consumers depend on that shape: an MCP App
 * bound to the call sends `ui/notifications/tool-input` only once `args` is an
 * object, and the host's result fetch reads its arguments from it.
 *
 * The call is addressed by an explicit `toolUseId`, never by "whatever is
 * current". The hook used to read its mutable `currentToolUseId` inside the
 * `setState` updater and null it right after — but React runs functional updaters
 * lazily, so whenever the stop arrived in the same chunk as a burst of deltas the
 * updater ran after the reset, matched nothing, and the arguments stayed a string
 * forever (the later `messageStop` settle only flips status). Live, the card app
 * received `tool-input` with `{}` and the host's fallback re-called the tool with
 * `{}`, which its schema rejected. Taking the id as a parameter forces the caller
 * to snapshot it before the updater is queued.
 *
 * Arguments that fail to parse are left as text: the raw string is still the most
 * faithful record of what the model wrote, and downstream renders it as such.
 *
 * Returns the original array when the call is not found, so React can skip the
 * re-render.
 *
 * @template M
 * @param {M[] | undefined} messages
 * @param {string | null | undefined} messageId  message the block belongs to
 * @param {string | null | undefined} toolUseId  the call the block closes
 * @returns {M[]}
 */
export function closeToolUse(messages, messageId, toolUseId) {
  if (!Array.isArray(messages)) return [];
  if (!messageId || !toolUseId) return messages;

  const index = messages.findIndex(
    (message) => message?.id === messageId && message?.type === "ai",
  );
  if (index < 0) return messages;

  const message = messages[index];
  const toolCalls = message.tool_calls;
  if (!Array.isArray(toolCalls)) return messages;

  const callIndex = toolCalls.findIndex((toolCall) => toolCall?.id === toolUseId);
  if (callIndex < 0) return messages;

  const toolCall = toolCalls[callIndex];
  let args = toolCall.args;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      // Keep the text: an unparseable body is still what the model wrote.
    }
  }

  const next = [...messages];
  next[index] = {
    ...message,
    tool_calls: toolCalls.map((tc, j) =>
      j === callIndex ? { ...tc, args, status: "completed" } : tc,
    ),
  };
  return next;
}
