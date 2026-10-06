"use client";

import useSWR from "swr";
import { getRegistryRecord, listRegistryRecords } from "@/lib/registry";
import { getCapabilities } from "@/lib/capabilities";
import {
  BASIC_CHAT_NAME,
  BASIC_CHAT_RECORD_ID,
  basicChatModelLabel,
} from "@/lib/basicChat.mjs";
import type { SelectedAgent } from "@/lib/config";
import { ApiClient } from "@/lib/api-client";
import type { Thread } from "@/lib/api-types";
// The decisions live in a plain module so they can be tested without a DOM or a
// React renderer — see threadAgentState.test.mjs.
import {
  classifyLookupFailure,
  resolveThreadAgentState,
} from "./threadAgentState.mjs";

/** Why a thread cannot be talked to. */
export type ThreadAgentProblem =
  /** Pinned to a registry record that no longer resolves (deleted, or revoked). */
  | "unresolvable"
  /** Written before pinning existed: which agent wrote its history is unknown. */
  | "unpinned"
  /** A basic-chat thread with no recorded model: continuing it could switch models unnoticed. */
  | "legacy-basic";

export interface ThreadAgentResult {
  /** The agent to invoke and to name in the header, or null while unresolved. */
  agent: SelectedAgent | null;
  /** Set when the thread cannot be continued; `agent` is null in that case. */
  problem: ThreadAgentProblem | null;
  loading: boolean;
}

/**
 * The agent a chat should be driven by.
 *
 * An open thread is pinned to one registry agent (`Thread.agent_record_id`) and
 * the server refuses a turn from any other with a 409, so the chat has to follow
 * the *thread's* agent rather than whichever one is selected in the sidebar.
 * Using the selection would send a turn the server rejects, and would label the
 * transcript with an agent that never wrote it.
 *
 * With no thread open the selection is correct: that is what a new chat starts.
 *
 * The record is fetched rather than trusted from the thread because only the ARNs
 * on the live record can actually invoke it, and a harness that was recomposed
 * has a new one. `agent_name` on the thread is a label for the sidebar, not a
 * substitute for the record.
 *
 * A failed lookup is *not* automatically an answer. This used to swallow every
 * failure into `{problem:"unresolvable"}` — a resolved value, which switched off
 * SWR's retrying, since only rejections are retried. One busy moment on the
 * server therefore pinned "이 대화의 에이전트를 찾을 수 없어 이어갈 수
 * 없습니다" for the whole session on a thread whose answer had streamed fine and
 * been saved. Now only a permanent failure resolves; a transient one is rethrown
 * so SWR retries it with backoff.
 */
export function useThreadAgent(
  threadId: string | null,
  selected: SelectedAgent | undefined,
  apiClient: ApiClient
): ThreadAgentResult {
  // Keyed on the thread, not the record: the pin is what this resolves, and it
  // is only known after the thread loads.
  const { data, error, isLoading } = useSWR(
    threadId ? ["thread-agent", threadId] : null,
    async ([, id]: [string, string]) => {
      const thread = (await apiClient.getThread(id)) as Thread;
      const recordId = thread.agent_record_id;
      if (!recordId) return { problem: "unpinned" as const };

      // Basic chat has no registry record; the thread's own model is the pin,
      // and it must still be on the server's allow-list to be invoked.
      if (recordId === BASIC_CHAT_RECORD_ID) {
        const modelId = thread.basic_chat_model_id;
        if (!modelId) return { problem: "legacy-basic" as const };
        const basic = (await getCapabilities()).basicChat;
        if (!basic?.configured || !basic.models.includes(modelId)) {
          return { problem: "unresolvable" as const };
        }
        return {
          agent: {
            recordId,
            name: BASIC_CHAT_NAME,
            description: `${basicChatModelLabel(modelId)} · 에이전트 없이 모델과 바로 대화`,
            basicChat: true,
            basicChatModelId: modelId,
          } satisfies SelectedAgent,
        };
      }

      // Deployed-fallback pins (registry off) have no registry record to GET.
      // Resolve them from the records listing, which returns the same
      // live-resource fallback the pin was made from.
      if (recordId.startsWith("deployed:")) {
        const records = await listRegistryRecords();
        const match = records.find((r) => r.record_id === recordId);
        if (!match) return { problem: "unresolvable" as const };
        return {
          agent: {
            recordId: match.record_id,
            name: match.name,
            description: match.description ?? undefined,
            agentRuntimeArn: match.agent_runtime_arn ?? undefined,
            harnessArn: match.harness_arn ?? undefined,
            qualifier: match.qualifier ?? undefined,
          } satisfies SelectedAgent,
        };
      }

      try {
        const record = await getRegistryRecord(recordId);
        return {
          agent: {
            recordId: record.record_id,
            name: record.name,
            description: record.description ?? undefined,
            agentRuntimeArn: record.agent_runtime_arn ?? undefined,
            harnessArn: record.harness_arn ?? undefined,
            qualifier: record.qualifier ?? undefined,
          } satisfies SelectedAgent,
        };
      } catch (err) {
        const failure = err as { status?: number; detail?: string; message?: string };
        const shape = {
          status: failure.status,
          detail: failure.detail ?? failure.message ?? "",
        };
        // Deleted, or no longer visible to this caller: final. Resolving rather
        // than throwing is what stops SWR retrying, and here that is correct —
        // the thread stays readable (it renders from DynamoDB) but nothing can
        // answer in it.
        if (classifyLookupFailure(shape) === "permanent") {
          return { problem: "unresolvable" as const };
        }
        // Anything else is worth another go. Rethrown so SWR retries with
        // backoff instead of treating a passing failure as the answer.
        throw err;
      }
    },
    // The pin itself never changes, so there is nothing to poll for — but a
    // failed *lookup* is retried (SWR's default), which is what recovers a
    // thread that would otherwise be stuck read-only.
    { revalidateOnFocus: false }
  );

  return resolveThreadAgentState({
    threadId,
    selected,
    data,
    error,
    isLoading,
  }) as ThreadAgentResult;
}
