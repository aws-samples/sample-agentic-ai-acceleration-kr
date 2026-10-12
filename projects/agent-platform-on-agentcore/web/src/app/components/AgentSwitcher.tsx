"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryState } from "nuqs";
import { useDefaultAgent } from "@/app/hooks/useDefaultAgent";
import {
  Bot,
  Check,
  ChevronDown,
  Library,
  Loader2,
  Search,
} from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAppShell } from "@/app/components/AppShell";
import { useThreadAgent } from "@/app/hooks/useThreadAgent";
import { useClient } from "@/providers/ClientProvider";
import {
  isChattable,
  listRegistryRecords,
  type RegistryRecordSummary,
} from "@/lib/registry";
import { modelLabel } from "@/lib/modelLabel.mjs";
import { useChatContext } from "@/providers/ChatProvider";

/**
 * Switches the agent a chat is bound to, in place.
 *
 * Selecting from here writes the same config the Registry page writes, so the
 * two stay interchangeable — but without leaving the conversation, which was
 * the only way to change agents before.
 */
export function AgentSwitcher({ className }: { className?: string }) {
  const router = useRouter();
  const { config, saveConfig } = useAppShell();
  const [threadId, setThreadId] = useQueryState("threadId");
  const apiClient = useClient();

  // Show whoever is actually answering. In an open thread that is the thread's
  // pinned agent, not the sidebar selection — labelling this with the selection
  // would contradict the header and imply a turn would reach an agent the server
  // will refuse. SWR dedupes this with the page's identical request.
  const { agent: threadAgent, problem } = useThreadAgent(
    threadId,
    config?.selectedAgent,
    apiClient
  );
  // On a thread that cannot be continued there is no agent to name. Falling back
  // to the selection here would put a confident agent name on a chat whose header
  // says it cannot be continued, and imply picking that same agent is a no-op —
  // when in fact it starts a new thread.
  // With nothing picked, the default agent is what a new chat will reach, so
  // the trigger names it rather than reading "Select agent" above a chat whose
  // header already says who answers. The saved config stays untouched.
  const { defaultAgent } = useDefaultAgent();
  const selected = problem
    ? null
    : (threadAgent ?? config?.selectedAgent ?? defaultAgent);
  // The model this thread overrides with, shown beside the pinned default agent
  // so the row says what will answer. Same source the override popover edits.
  const { overrides } = useChatContext();

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [records, setRecords] = useState<RegistryRecordSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Load once per open so a newly approved agent shows up without a reload.
  // The filter is isChattable, not a status check: an edited record is DRAFT but
  // still discoverable, and must stay in the picker while its approved revision
  // serves. Listing unapproved records would offer a choice that can only end in
  // a 403, which isChattable already excludes.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    listRegistryRecords()
      .then((all) => {
        if (!cancelled) setRecords(all.filter(isChattable));
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setRecords([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Filter locally: the list is small, and the hybrid search API caps at 20
  // relevance-ranked results, which for a picker means an agent can vanish
  // just because the query ranked it out.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matching = (records ?? []).filter(
      (r) =>
        !q ||
        r.name.toLowerCase().includes(q) ||
        (r.description ?? "").toLowerCase().includes(q)
    );
    // The default agent first: it is what a new chat starts with when nothing
    // is picked, so it is the row people look for. Registry order after that.
    return [...matching.filter((r) => r.is_default), ...matching.filter((r) => !r.is_default)];
  }, [records, query]);

  const select = useCallback(
    (record: RegistryRecordSummary) => {
      setOpen(false);
      setQuery("");
      if (record.record_id === selected?.recordId) return;
      saveConfig({
        ...(config ?? {}),
        selectedAgent: {
          recordId: record.record_id,
          name: record.name,
          description: record.description ?? undefined,
          agentRuntimeArn: record.agent_runtime_arn ?? undefined,
          harnessArn: record.harness_arn ?? undefined,
          qualifier: record.qualifier ?? undefined,
        },
      });
      // Start a new thread. A thread is pinned to one agent server-side, so
      // keeping it would make the next turn a 409 — and the reason it is pinned
      // is AgentCore Memory: the session id is derived from the thread id alone,
      // so two agents in one thread either share an event stream or lose the
      // history to each other's memory (see Thread.agent_record_id). The runtime
      // session id being thread-derived means they would share a sandbox too.
      void setThreadId(null);
    },
    [config, saveConfig, selected?.recordId, setThreadId]
  );

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery("");
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex h-8 max-w-[240px] items-center gap-1.5 rounded-full border border-border",
            "bg-card px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-accent",
            className
          )}
          title="Change agent"
        >
          <Bot className="h-3.5 w-3.5 flex-shrink-0" />
          <span className="min-w-0 truncate">
            {selected?.name ?? "Select agent"}
          </span>
          <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>

      <PopoverContent align="end" side="top" className="w-80 p-0">
        <div className="relative border-b p-2">
          <Search className="absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search agents..."
            className="h-8 pl-8 text-sm"
          />
        </div>

        <div className="max-h-72 overflow-y-auto p-1">
          {records === null ? (
            <div className="flex items-center justify-center gap-2 p-4 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Loading agents...
            </div>
          ) : error ? (
            <p className="p-3 text-xs text-destructive">{error}</p>
          ) : filtered.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground">
              {query.trim()
                ? "일치하는 에이전트가 없습니다."
                : "채팅 가능한 에이전트가 없습니다."}
            </p>
          ) : (
            filtered.map((record) => {
              const isSelected = record.record_id === selected?.recordId;
              return (
                <button
                  key={record.record_id}
                  type="button"
                  onClick={() => select(record)}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left transition-colors",
                    "hover:bg-accent",
                    isSelected && "bg-accent"
                  )}
                >
                  <Check
                    className={cn(
                      "mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-primary",
                      !isSelected && "invisible"
                    )}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">
                        {record.name}
                      </span>
                      <span className="flex-shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                        {record.harness_arn ? "Harness" : "Runtime"}
                      </span>
                      {record.is_default && (
                        <span className="flex-shrink-0 rounded-sm bg-primary/10 px-1 text-[10px] uppercase tracking-wide text-primary">
                          Default
                        </span>
                      )}
                      {record.source === "deployed" && (
                        <span className="text-[10px] uppercase tracking-wide opacity-60">Deployed</span>
                      )}
                    </span>
                    {record.is_default && isSelected && overrides.modelId ? (
                      <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                        {modelLabel(overrides.modelId)}
                      </span>
                    ) : (
                      record.description && (
                        <span className="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">
                          {record.description}
                        </span>
                      )
                    )}
                  </span>
                </button>
              );
            })
          )}
        </div>

        <button
          type="button"
          onClick={() => {
            setOpen(false);
            router.push("/registry");
          }}
          className="flex w-full items-center gap-2 border-t px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <Library className="h-3.5 w-3.5" />
          Browse Registry
        </button>
      </PopoverContent>
    </Popover>
  );
}
