"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryState } from "nuqs";
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
import { getCapabilities, type BasicChatCapability } from "@/lib/capabilities";
import {
  BASIC_CHAT_NAME,
  basicChatAgent,
  basicChatModelLabel,
} from "@/lib/basicChat.mjs";

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
  const selected = problem ? null : (threadAgent ?? config?.selectedAgent);

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [records, setRecords] = useState<RegistryRecordSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Basic chat sits above the registry list: one row per allowed model. The
  // allow-list is the server's, fetched on open so an operator change shows up.
  const [basicChat, setBasicChat] = useState<BasicChatCapability | undefined>();

  // Load once per open so a newly approved agent shows up without a reload.
  // Approved-only, both here and in isChattable: the server refuses to bind a
  // thread to an unapproved record, so listing one would offer a choice that
  // can only end in a 403.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    getCapabilities()
      .then((caps) => {
        if (!cancelled) setBasicChat(caps.basicChat ?? { configured: false, models: [] });
      })
      .catch(() => {
        if (!cancelled) setBasicChat({ configured: false, models: [] });
      });
    listRegistryRecords({ status: "APPROVED" })
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
    if (!q) return records ?? [];
    return (records ?? []).filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        (r.description ?? "").toLowerCase().includes(q)
    );
  }, [records, query]);

  const basicModels = useMemo(() => {
    if (!basicChat?.configured) return [];
    const q = query.trim().toLowerCase();
    return basicChat.models.filter(
      (m) =>
        !q ||
        BASIC_CHAT_NAME.includes(q) ||
        m.toLowerCase().includes(q) ||
        basicChatModelLabel(m).toLowerCase().includes(q)
    );
  }, [basicChat, query]);

  const selectBasic = useCallback(
    (modelId: string) => {
      setOpen(false);
      setQuery("");
      if (selected?.basicChat && selected.basicChatModelId === modelId) return;
      const agent = basicChatAgent(basicChat, modelId);
      if (!agent) return;
      saveConfig({ ...(config ?? {}), selectedAgent: agent });
      // Same reason as `select` below: a thread is pinned to one agent — and a
      // basic-chat thread to one model — so changing either starts a new one.
      void setThreadId(null);
    },
    [basicChat, config, saveConfig, selected?.basicChat, selected?.basicChatModelId, setThreadId]
  );

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
            {selected?.basicChat
              ? `${selected.name} · ${basicChatModelLabel(selected.basicChatModelId)}`
              : (selected?.name ?? "Select agent")}
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
          {basicModels.length > 0 && (
            <div className="mb-1 border-b pb-1">
              <p className="px-2 pb-1 pt-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                {BASIC_CHAT_NAME}
              </p>
              {basicModels.map((modelId) => {
                const isSelected =
                  !!selected?.basicChat && selected.basicChatModelId === modelId;
                return (
                  <button
                    key={modelId}
                    type="button"
                    onClick={() => selectBasic(modelId)}
                    data-basic-model={modelId}
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
                      <span className="block truncate text-sm font-medium">
                        {basicChatModelLabel(modelId)}
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                        {modelId}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          {records === null ? (
            <div className="flex items-center justify-center gap-2 p-4 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Loading agents...
            </div>
          ) : error ? (
            <p className="p-3 text-xs text-destructive">{error}</p>
          ) : filtered.length === 0 ? (
            basicModels.length === 0 && (
              <p className="p-3 text-xs text-muted-foreground">
                {query.trim()
                  ? "일치하는 에이전트가 없습니다."
                  : "채팅 가능한 에이전트가 없습니다."}
              </p>
            )
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
                      {record.source === "deployed" && (
                        <span className="text-[10px] uppercase tracking-wide opacity-60">Deployed</span>
                      )}
                    </span>
                    {record.description && (
                      <span className="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">
                        {record.description}
                      </span>
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
