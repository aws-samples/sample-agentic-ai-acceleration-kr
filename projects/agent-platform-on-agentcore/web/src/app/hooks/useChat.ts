"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { getCapabilities } from "@/lib/capabilities";
import { useStream } from "@/hooks/useStream";
import type { Message, Checkpoint } from "@/lib/api-types";
import { listThreadArtifacts, type ArtifactEvent } from "@/lib/artifacts";
import { v4 as uuidv4 } from "uuid";
import type { TodoItem } from "@/app/types/types";
import { useClient } from "@/providers/ClientProvider";
import { HumanResponse } from "@/app/types/inbox";
import { useQueryState } from "nuqs";
import type { AttachmentRef } from "@/lib/attachments";
import {
  applyAgentConfig,
  applyOverrides,
  overridesFromMetadata,
  OVERRIDES_METADATA_KEY,
  type AgentChatConfig,
  type ThreadOverrides,
} from "@/lib/agent-config";
import type { Thread } from "@/lib/api-types";

export type StateType = {
  messages: Message[];
  todos: TodoItem[];
  files: Record<string, string>;
  email?: {
    id?: string;
    subject?: string;
    page_content?: string;
  };
  mcpApps?: any;
  artifacts?: ArtifactEvent[];
};

// Re-export for backward compatibility
export type { AgentChatConfig };

export function useChat({
  onHistoryRevalidate,
  agentConfig,
}: {
  onHistoryRevalidate?: () => void;
  agentConfig?: AgentChatConfig;
}) {
  const [threadId, setThreadId] = useQueryState("threadId");
  const apiClient = useClient();

  // Per-thread model / prompt overrides. State here so the popover and the send
  // path see the same value; persisted in thread metadata so the thread keeps
  // them when reopened. Offered for any agent the server can forward them to:
  // a harness (InvokeHarness model/systemPrompt) or a runtime (invoke payload).
  const [overrides, setOverridesState] = useState<ThreadOverrides>({});
  // Set before the thread exists (a fresh chat): the PATCH has to wait until the
  // first turn has created the thread, or it lands on a 404.
  const pendingOverridesRef = useRef<ThreadOverrides | null>(null);
  const overridesSupported = !!(agentConfig?.harnessArn || agentConfig?.agentRuntimeArn);

  const flushPendingOverrides = useCallback(
    (id: string | null) => {
      const pending = pendingOverridesRef.current;
      if (!pending || !id) return;
      pendingOverridesRef.current = null;
      void apiClient
        .updateThreadState(id, { metadata: { [OVERRIDES_METADATA_KEY]: pending } })
        .catch(() => {
          // Best effort: the turn already ran with the overrides in its config.
          // Losing the persisted copy only costs a re-set on reopen.
        });
    },
    [apiClient]
  );

  const stream = useStream<StateType>({
    threadId: threadId ?? null,
    apiClient,
    onThreadId: setThreadId,
    onFinish: () => {
      // The thread now exists server-side, so anything set before the first
      // turn can be written down.
      flushPendingOverrides(threadIdRef.current);
      onHistoryRevalidate?.();
    },
    onError: onHistoryRevalidate,
    onCreated: onHistoryRevalidate,
  });

  // The URL's thread id, readable from callbacks created before it changed.
  const threadIdRef = useRef<string | null>(threadId ?? null);
  threadIdRef.current = threadId ?? null;

  // Seed the overrides from the thread when one is opened. Same GET the agent
  // hook makes, deduped by SWR only if the keys match — they don't, and this
  // one is cheap. A new chat (no thread) starts clean.
  const { data: storedOverrides } = useSWR(
    threadId ? ["thread-overrides", threadId] : null,
    async ([, id]: [string, string]) => {
      const thread = (await apiClient.getThread(id)) as Thread;
      const stored = overridesFromMetadata(thread.metadata);
      // A thread the retired basic-chat path pinned carries its model in a
      // field of its own. Seeding the override from it means this session
      // keeps sending that model while the server moves the thread onto the
      // default record (which also writes the override for later reopens).
      // Only a model the server still allows is seeded: a withdrawn one is
      // dropped server-side anyway, and sending it would read as a choice.
      if (!stored.modelId && thread.agent_record_id === "__basic_chat__" && thread.basic_chat_model_id) {
        const allowed = (await getCapabilities()).allowedModels ?? [];
        if (allowed.includes(thread.basic_chat_model_id)) {
          return { ...stored, modelId: thread.basic_chat_model_id };
        }
      }
      return stored;
    },
    { revalidateOnFocus: false }
  );
  useEffect(() => {
    if (!threadId) {
      setOverridesState({});
      return;
    }
    // A thread this session just created (pending flushed on finish) has
    // nothing stored yet; keep what the user set rather than blanking it.
    if (storedOverrides && !pendingOverridesRef.current) {
      setOverridesState(storedOverrides);
    }
  }, [threadId, storedOverrides]);

  const setOverrides = useCallback(
    (next: ThreadOverrides) => {
      setOverridesState(next);
      if (threadId) {
        void apiClient
          .updateThreadState(threadId, {
            metadata: { [OVERRIDES_METADATA_KEY]: next },
          })
          .catch(() => {
            // See flushPendingOverrides: the in-memory value still drives the
            // next turn; only persistence is lost.
          });
      } else {
        pendingOverridesRef.current = next;
      }
    },
    [apiClient, threadId]
  );

  // Artifacts persisted for this thread. A reopened thread has no streamed
  // artifacts, so without this the panel loses its cards and version history.
  const { data: storedArtifacts } = useSWR(
    threadId ? ["thread-artifacts", threadId] : null,
    async ([, id]: [string, string]) => {
      try {
        return await listThreadArtifacts(id);
      } catch {
        // Artifact storage may be unconfigured; streamed artifacts still render.
        return [];
      }
    }
  );

  const streamedArtifacts = (stream.values as any)?.artifacts as
    | ArtifactEvent[]
    | undefined;

  const artifacts = useMemo<ArtifactEvent[]>(() => {
    const merged = new Map<string, ArtifactEvent>();
    for (const a of storedArtifacts ?? []) {
      merged.set(`${a.artifact_id}:${a.version}`, {
        artifactId: a.artifact_id,
        version: a.version,
        threadId: a.thread_id,
        title: a.title,
        kind: a.kind,
        language: a.language ?? undefined,
        filename: a.filename ?? undefined,
        toolCallId: a.tool_call_id ?? undefined,
        messageId: a.message_id ?? undefined,
        s3Key: a.s3_key,
        sizeBytes: a.size_bytes,
        createdAt: a.created_at,
        stored: true,
      });
    }
    // Streamed entries win: they carry the inline content.
    for (const a of streamedArtifacts ?? []) {
      merged.set(`${a.artifactId}:${a.version}`, a);
    }
    return [...merged.values()].sort((x, y) =>
      (x.createdAt ?? "").localeCompare(y.createdAt ?? "")
    );
  }, [storedArtifacts, streamedArtifacts]);

  // MCP App 이 `ui/update-model-context` 로 준 보조 컨텍스트. 규격: 다음 사용자
  // 메시지 전에 여러 번 오면 마지막 것만 모델에게 전달한다 — 그래서 덮어쓰는 ref 하나다.
  // 전송 시 config 에 실어 보내고 비운다. 저장되는 human 메시지에는 넣지 않는다(서버가
  // 모델로 나가는 사본에만 붙인다).
  const appModelContextRef = useRef<Record<string, unknown> | null>(null);
  const setAppModelContext = useCallback((context: Record<string, unknown> | null) => {
    appModelContextRef.current = context;
  }, []);

  const sendMessage = useCallback(
    (content: string, attachments?: AttachmentRef[]) => {
      // With attachments the content becomes a block array: the text plus one
      // small reference per file. No bytes — the server resolves each reference
      // from S3 when it builds the runtime payload, so re-sending the whole
      // conversation every turn stays cheap.
      const messageContent =
        attachments && attachments.length > 0
          ? [{ type: "text", text: content }, ...attachments]
          : content;

      const newMessage: Message = {
        id: uuidv4(),
        type: "human",
        content: messageContent as Message["content"],
      };

      // Always send the full conversation. The agent side is stateless as far as
      // history goes — harness memory is disabled for composed agents — so the
      // request has to carry the context itself.
      const allMessages = [...stream.messages, newMessage];

      // Immediately add the message to the UI
      if (stream.addMessage) {
        stream.addMessage(newMessage);
      }

      const config: Record<string, unknown> = applyOverrides(
        applyAgentConfig({ recursion_limit: 100 }, agentConfig),
        overridesSupported ? overrides : null
      );
      if (appModelContextRef.current) {
        config.app_model_context = appModelContextRef.current;
        appModelContextRef.current = null;
      }

      stream.submit({ messages: allMessages }, { config });
      // Update thread list immediately when sending a message
      onHistoryRevalidate?.();
    },
    [stream, onHistoryRevalidate, agentConfig, overrides, overridesSupported]
  );

  const runSingleStep = useCallback(
    (
      messages: Message[],
      checkpoint?: Checkpoint,
      isRerunningSubagent?: boolean,
      optimisticMessages?: Message[]
    ) => {
      const baseConfig = applyOverrides(
        applyAgentConfig({}, agentConfig),
        overridesSupported ? overrides : null
      );

      if (checkpoint) {
        stream.submit(undefined, {
          ...(optimisticMessages
            ? {
                optimisticValues: (prev: StateType) => ({
                  ...prev,
                  messages: optimisticMessages,
                }),
              }
            : {}),
          config: baseConfig,
          checkpoint: checkpoint,
          ...(isRerunningSubagent
            ? { interruptAfter: ["tools"] }
            : { interruptBefore: ["tools"] }),
        });
      } else {
        stream.submit(
          { messages },
          { config: baseConfig, interruptBefore: ["tools"] }
        );
      }
    },
    [stream, agentConfig, overrides, overridesSupported]
  );

  const setFiles = useCallback(
    async (files: Record<string, string>) => {
      if (!threadId) return;
      await apiClient.updateThreadState(threadId, { values: { files } });
    },
    [apiClient, threadId]
  );

  const continueStream = useCallback(
    (hasTaskToolCall?: boolean) => {
      const baseConfig = applyOverrides(
        applyAgentConfig({ recursion_limit: 100 }, agentConfig),
        overridesSupported ? overrides : null
      );

      stream.submit(undefined, {
        config: baseConfig,
        ...(hasTaskToolCall
          ? { interruptAfter: ["tools"] }
          : { interruptBefore: ["tools"] }),
      });
      // Update thread list when continuing stream
      onHistoryRevalidate?.();
    },
    [stream, agentConfig, onHistoryRevalidate, overrides, overridesSupported]
  );

  const sendHumanResponse = useCallback(
    (response: HumanResponse[]) => {
      stream.submit(null, { command: { resume: response } });
      // Update thread list when resuming from interrupt
      onHistoryRevalidate?.();
    },
    [stream, onHistoryRevalidate]
  );

  const markCurrentThreadAsResolved = useCallback(() => {
    stream.submit(null, { command: { goto: "__end__", update: null } });
    // Update thread list when marking thread as resolved
    onHistoryRevalidate?.();
  }, [stream, onHistoryRevalidate]);

  const stopStream = useCallback(() => {
    stream.stop();
  }, [stream]);

  return {
    stream,
    // The agent this chat is bound to. Exposed so the composer can gate on the
    // real target rather than the sidebar selection, which an open thread
    // deliberately overrides.
    agentConfig,
    // Per-thread model / prompt overrides (harness agents only).
    overrides,
    setOverrides,
    overridesSupported,
    todos: ((stream.values as any)?.todos ?? []) as TodoItem[],
    files: ((stream.values as any)?.files ?? {}) as Record<string, string>,
    email: (stream.values as any)?.email,
    mcpApps: (stream.values as any)?.mcpApps,
    artifacts,
    setFiles,
    messages: stream.messages,
    isLoading: stream.isLoading,
    isThreadLoading: stream.isThreadLoading,
    // A run refused before it started — e.g. a 409 from addressing this thread's
    // turn to a different agent than the one it is pinned to.
    error: stream.error,
    // What the agent is doing while it emits no text, or undefined once the
    // answer starts arriving. Transient — never part of the transcript.
    status: stream.status,
    interrupt: stream.interrupt,
    getMessagesMetadata: stream.getMessagesMetadata || (() => ({})),
    sendMessage,
    setAppModelContext,
    runSingleStep,
    continueStream,
    stopStream,
    sendHumanResponse,
    markCurrentThreadAsResolved,
  };
}
