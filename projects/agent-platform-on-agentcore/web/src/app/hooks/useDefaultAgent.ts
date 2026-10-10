"use client";

import useSWR from "swr";

import type { SelectedAgent } from "@/lib/config";
import { isChattable, listRegistryRecords } from "@/lib/registry";

/**
 * The default agent — the registry record flagged `is_default` — as a
 * `SelectedAgent`, or `null` when there is none that can be chatted with.
 *
 * It stands in wherever nothing is selected: the chat page invokes it for a new
 * chat, and the switcher names it, so a first visit does not say "Select agent"
 * above a chat that is about to be answered by this very agent. Nothing is
 * written to the saved config: a fallback is not a choice, and writing it would
 * turn "whatever the default is today" into a pin on one record.
 *
 * One SWR key, so the page and the switcher share a single request.
 */
export function useDefaultAgent() {
  const { data, isLoading } = useSWR(
    "default-agent",
    async (): Promise<SelectedAgent | null> => {
      const records = await listRegistryRecords();
      const match = records.find((r) => r.is_default && isChattable(r));
      return match
        ? {
            recordId: match.record_id,
            name: match.name,
            description: match.description ?? undefined,
            agentRuntimeArn: match.agent_runtime_arn ?? undefined,
            harnessArn: match.harness_arn ?? undefined,
            qualifier: match.qualifier ?? undefined,
          }
        : null;
    },
    { revalidateOnFocus: false }
  );
  return { defaultAgent: data ?? null, defaultLoading: isLoading };
}
