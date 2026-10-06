/**
 * Settling still-pending tool calls when a stream ends — the rule, separated
 * from the hook.
 *
 * Plain `.mjs` so `node --test` can exercise it without a DOM or a React
 * renderer, following threadAgentState.mjs. See settleToolCalls.test.mjs for the
 * failure this exists to prevent.
 */

/**
 * The same conversation with nothing left claiming to be running.
 *
 * The stream ending is the signal. A tool call is rendered as a spinner while its
 * status is "pending", and the normal way out is its `toolResult` or the
 * `messageStop` that closes the message. Neither arrives when the connection
 * drops mid-turn, and there is no later event that could — so the end of the
 * stream, however it ended, is the last chance to say the call is no longer
 * running.
 *
 * A duration cannot be used instead: on the deployed ks_text2sql_agent a
 * `sql_specialist` call sits pending for up to 102s while an earlier parallel call
 * finishes, so any timeout short enough to be useful would settle calls that are
 * still working.
 *
 * Only "pending" is rewritten. "interrupted" is already a settled state that says
 * something more specific, and no `result` is invented: the call really did end
 * without reporting one, and claiming otherwise would put words in the tool's
 * mouth. The UI shows a completed call with no output, which is what happened.
 *
 * Returns the original array when nothing was pending — the common case, since
 * most turns end cleanly — so React can skip the re-render.
 *
 * Typed as a pass-through: `status` is tracked on tool calls at runtime but is not
 * part of the declared `Message` shape (the hook casts for it elsewhere), so
 * constraining the parameter here would reject the very array it is called with.
 *
 * @template M
 * @param {M[] | undefined} messages
 * @returns {M[]}
 */
export function settlePendingToolCalls(messages) {
  if (!Array.isArray(messages)) return [];

  let changed = false;
  const next = messages.map((message) => {
    const toolCalls = message?.tool_calls;
    if (!Array.isArray(toolCalls) || !toolCalls.some((t) => t?.status === "pending")) {
      return message;
    }
    changed = true;
    return {
      ...message,
      tool_calls: toolCalls.map((toolCall) =>
        toolCall?.status === "pending"
          ? { ...toolCall, status: "completed" }
          : toolCall
      ),
    };
  });

  return changed ? next : messages;
}
