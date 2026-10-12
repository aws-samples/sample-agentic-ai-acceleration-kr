/**
 * Decides which message each artifact card hangs off.
 *
 * The chat renders a card only inside a message, so an artifact that matches no
 * message is not "shown somewhere else" — it is invisible. That was the whole
 * failure of the harness sweep: it stored files with no `messageId` (the stream
 * clears its message tracking at `messageStop`, and the sweep runs after), so the
 * agent answered "보고서를 작성했습니다", the row and the S3 object both existed,
 * the download route served it — and the chat showed nothing at all.
 *
 * The server now names the message, but two cases keep this fallback necessary:
 * rows already stored without one, and a recovery sweep for a turn whose message
 * id could not be recovered. Both land on the last AI message, which is the turn
 * the file belongs to.
 *
 * A card cannot go on a human message (ChatMessage renders artifacts only for
 * `!isUser`), so an orphan in a thread with no AI message stays unplaced. That
 * combination means the agent produced a file and no message at all.
 */

/**
 * @template {{messageId?: string, toolCallId?: string}} T
 * @param {Array<{id?: string, type?: string, toolCallIds?: string[]}>} messages
 *   In render order.
 * @param {T[] | undefined} artifacts
 * @returns {T[][]} one bucket per message, same order.
 */
export function assignArtifactsToMessages(messages, artifacts) {
  const buckets = messages.map(() => []);
  if (!artifacts?.length) return buckets;

  const indexById = new Map();
  const indexByToolCall = new Map();
  let lastAiIndex = -1;
  messages.forEach((message, index) => {
    if (message?.id !== undefined && message?.id !== null) {
      indexById.set(message.id, index);
    }
    for (const toolCallId of message?.toolCallIds ?? []) {
      indexByToolCall.set(toolCallId, index);
    }
    // "not human" rather than "=== ai": the streamed shape uses `ai`, but a
    // message rebuilt from storage has been seen without a type at all, and an
    // untyped message renders as an assistant turn.
    if (message?.type !== "human") lastAiIndex = index;
  });

  for (const artifact of artifacts) {
    let target = -1;
    if (artifact?.messageId !== undefined && indexById.has(artifact.messageId)) {
      target = indexById.get(artifact.messageId);
    } else if (
      artifact?.toolCallId !== undefined &&
      indexByToolCall.has(artifact.toolCallId)
    ) {
      target = indexByToolCall.get(artifact.toolCallId);
    } else if (!artifact?.messageId && !artifact?.toolCallId) {
      // Only a *nameless* artifact falls back. One that names a message absent
      // from this render (a stale row, another thread's) must not be relabelled
      // as belonging to the latest turn.
      target = lastAiIndex;
    }
    if (target >= 0) buckets[target].push(artifact);
  }

  return buckets;
}
