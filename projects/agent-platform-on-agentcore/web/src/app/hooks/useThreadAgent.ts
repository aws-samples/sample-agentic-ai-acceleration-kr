"use client";

import useSWR from "swr";
import { getRegistryRecord, isChattable, listRegistryRecords } from "@/lib/registry";
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
  | "unpinned";

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

      // A thread the retired basic-chat path pinned. The server moves it onto
      // the default agent's record on its next turn (services/agent_access.py);
      // until then, drive the chat with that record so the header, the picker and
      // the turn all name the agent that will actually answer. The pin itself is
      // not rewritten here — the server owns it.
      if (recordId === "__basic_chat__") {
        const records = await listRegistryRecords();
        const match = records.find((r) => r.is_default && isChattable(r));
        if (!match) return { problem: "unresolvable" as const };
        return { agent: toSelectedAgent(match) };
      }

      // Deployed-fallback pins (registry off) have no registry record to GET.
      // Resolve them from the records listing, which returns the same
      // live-resource fallback the pin was made from.
      if (recordId.startsWith("deployed:")) {
        const records = await listRegistryRecords();
        const match = records.find((r) => r.record_id === recordId);
        if (!match) return { problem: "unresolvable" as const };
        return { agent: toSelectedAgent(match) };
      }

      try {
        const record = await getRegistryRecord(recordId);
        return { agent: toSelectedAgent(record) };
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

function toSelectedAgent(record: {
  record_id: string;
  name: string;
  description?: string | null;
  agent_runtime_arn?: string | null;
  harness_arn?: string | null;
  qualifier?: string | null;
}): SelectedAgent {
  return {
    recordId: record.record_id,
    name: record.name,
    description: record.description ?? undefined,
    agentRuntimeArn: record.agent_runtime_arn ?? undefined,
    harnessArn: record.harness_arn ?? undefined,
    qualifier: record.qualifier ?? undefined,
  };
}
