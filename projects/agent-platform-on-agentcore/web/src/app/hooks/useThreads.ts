import useSWRInfinite from "swr/infinite";
import type { Thread, ThreadStatus } from "@/lib/api-types";
import { ApiClient } from "@/lib/api-client";
import { getConfig } from "@/lib/config";
import { epochOf } from "@/app/insights/threadRows.mjs";

/** Server `utcnow().isoformat()` (no zone) interpreted as UTC, as a Date. */
function parseUpdatedAt(iso: string): Date {
  const ms = epochOf(iso);
  return ms != null ? new Date(ms) : new Date(NaN);
}

export interface ThreadItem {
  id: string;
  updatedAt: Date;
  status: ThreadStatus;
  title: string;
  description: string;
  /**
   * Name of the agent this thread is pinned to, as recorded when it was pinned.
   * Empty on threads from before pinning existed, which cannot be continued.
   *
   * The stored name is used rather than resolving each record: the sidebar lists
   * twenty threads at a time, and a label is not worth twenty lookups. It also
   * still reads correctly for an agent that has since been deleted.
   */
  agentName: string;
}

const DEFAULT_PAGE_SIZE = 20;

export function useThreads(props: {
  status?: ThreadStatus;
  limit?: number;
}) {
  const pageSize = props.limit || DEFAULT_PAGE_SIZE;

  return useSWRInfinite(
    (pageIndex: number, previousPageData: ThreadItem[] | null) => {
      const config = getConfig();

      if (!config) {
        return null;
      }

      // If the previous page returned no items, we've reached the end
      if (previousPageData && previousPageData.length === 0) {
        return null;
      }

      return {
        kind: "threads" as const,
        pageIndex,
        pageSize,
        status: props?.status,
      };
    },
    async ({
      status,
      pageIndex,
      pageSize,
    }: {
      kind: "threads";
      pageIndex: number;
      pageSize: number;
      status?: ThreadStatus;
    }) => {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
      const apiClient = new ApiClient({ apiUrl });

      const threads = (await apiClient.searchThreads({
        limit: pageSize,
        offset: pageIndex * pageSize,
        sort_by: "updated_at",
        sort_order: "desc",
        status,
      })) as Thread[];

      return threads.map((thread: Thread): ThreadItem => {
        let title = "Untitled Thread";
        let description = "";

        try {
          if (thread.values && typeof thread.values === "object") {
            const values = thread.values as any;
            // Ensure messages is an array
            const messages = Array.isArray(values.messages) ? values.messages : [];
            
            // Find first human message for title
            const firstHumanMessage = messages.find(
              (m: any) => m && m.type === "human"
            );
            if (firstHumanMessage?.content) {
              const content =
                typeof firstHumanMessage.content === "string"
                  ? firstHumanMessage.content
                  : (Array.isArray(firstHumanMessage.content) && firstHumanMessage.content[0]?.text)
                    ? firstHumanMessage.content[0].text
                    : "";
              if (content) {
                title = content.slice(0, 50) + (content.length > 50 ? "..." : "");
              }
            }
            
            // Find first AI message for description
            const firstAiMessage = messages.find(
              (m: any) => m && m.type === "ai"
            );
            if (firstAiMessage?.content) {
              const content =
                typeof firstAiMessage.content === "string"
                  ? firstAiMessage.content
                  : (Array.isArray(firstAiMessage.content) && firstAiMessage.content[0]?.text)
                    ? firstAiMessage.content[0].text
                    : "";
              if (content) {
                description = content.slice(0, 100);
              }
            }
          }
        } catch (error) {
          // Fallback to thread ID
          console.warn("Error parsing thread title:", error);
          title = `Thread ${thread.thread_id.slice(0, 8)}`;
        }

      return {
        id: thread.thread_id,
        updatedAt: parseUpdatedAt(thread.updated_at),
        status: thread.status,
        title,
        description,
        agentName: thread.agent_name ?? "",
      };
      });
    },
    {
      revalidateFirstPage: true,
      revalidateOnFocus: true,
    }
  );
}
