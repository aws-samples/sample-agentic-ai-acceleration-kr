"use client";

import React, {
  useState,
  useRef,
  useCallback,
  useMemo,
  useEffect,
  FormEvent,
  Fragment,
} from "react";
import { Button } from "@/components/ui/button";
import {
  LoaderCircle,
  Square,
  ArrowUp,
  CheckCircle,
  Clock,
  Circle,
  FileIcon,
  Paperclip,
} from "lucide-react";
import { ChatMessage } from "@/app/components/ChatMessage";
import { assignArtifactsToMessages } from "@/app/components/messageArtifacts.mjs";
import type { TodoItem, ToolCall } from "@/app/types/types";
import type { Message, MessageContent } from "@/lib/api-types";
import {
  extractStringFromMessageContent,
  isPreparingToCallTaskTool,
} from "@/app/utils/utils";
import { useChatContext } from "@/providers/ChatProvider";
import { useQueryState } from "nuqs";
import { cn, randomId } from "@/lib/utils";
import { useStickToBottom } from "use-stick-to-bottom";
import { FilesPopover } from "@/app/components/TasksFilesSidebar";
import { AgentSwitcher } from "@/app/components/AgentSwitcher";
import { ThreadOverrides } from "@/app/components/ThreadOverrides";
import { AttachmentBar } from "@/app/components/AttachmentBar";
import { AgentStatusLine } from "@/app/components/AgentStatusLine";
import {
  ACCEPT_ATTRIBUTE,
  MAX_BYTES,
  MAX_FILES,
  isAllowed,
  uploadAttachment,
  type AttachmentRef,
  type PendingAttachment,
} from "@/lib/attachments";

interface ChatInterfaceProps {
  // Optional controlled view props from host app
  view?: "chat" | "workflow";
  onViewChange?: (view: "chat" | "workflow") => void;
  hideInternalToggle?: boolean;
  InterruptActionsRenderer?: React.ComponentType;
  onInput?: (input: string) => void;

  controls: React.ReactNode;
  banner?: React.ReactNode;
  skeleton: React.ReactNode;

  /** Artifact currently shown in the side panel, so its card reads as selected. */
  activeArtifact?: { artifactId: string; version: number } | null;
  onOpenArtifact?: (artifactId: string, version: number) => void;

  /**
   * Render the transcript but refuse to send.
   *
   * For a thread that cannot be continued — no agent recorded, or its record is
   * gone. The server would answer 409 anyway; this keeps the user from composing
   * a message only to have it bounce, while leaving the history readable.
   */
  readOnly?: boolean;
}

const getStatusIcon = (status: TodoItem["status"], className?: string) => {
  switch (status) {
    case "completed":
      return (
        <CheckCircle
          size={16}
          className={cn("text-success", className)}
        />
      );
    case "in_progress":
      return (
        <Clock
          size={16}
          className={cn("text-warning", className)}
        />
      );
    default:
      return (
        <Circle
          size={16}
          className={cn("text-muted-foreground", className)}
        />
      );
  }
};

export const ChatInterface = React.memo<ChatInterfaceProps>(
  ({
    view,
    onViewChange,
    onInput,
    controls,
    banner,
    hideInternalToggle,
    skeleton,
    activeArtifact,
    onOpenArtifact,
    readOnly,
  }) => {
    const [threadId] = useQueryState("threadId");
    const [agentId] = useQueryState("agentId");
    const [metaOpen, setMetaOpen] = useState<"tasks" | "files" | null>(null);
    const tasksContainerRef = useRef<HTMLDivElement | null>(null);
    const [isWorkflowView, setIsWorkflowView] = useState(false);

    const textareaRef = useRef<HTMLTextAreaElement | null>(null);
    const isClearingRef = useRef(false);
    const isControlledView = typeof view !== "undefined";
    const workflowView = isControlledView
      ? view === "workflow"
      : isWorkflowView;

    const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
    const [dragDepth, setDragDepth] = useState(0);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    const { agentConfig } = useChatContext();
    // A harness cannot receive binary at all: InvokeHarness's content block has
    // only text/toolUse/toolResult/reasoningContent. Disabling the control is
    // honest; accepting files and dropping them silently is not.
    //
    // Read off the agent this chat is actually bound to, not the sidebar
    // selection: an open thread follows its own pinned agent, so the selection
    // may be a runtime while the thread belongs to a harness — which would offer
    // an upload the harness then drops.
    const attachmentsSupported = !readOnly && !agentConfig?.harnessArn;

    // Uploads target the current thread, or the one this turn will create.
    const pendingThreadIdRef = useRef<string | null>(null);

    useEffect(() => {
      const timeout = setTimeout(() => void textareaRef.current?.focus());
      return () => clearTimeout(timeout);
    }, [threadId, agentId]);

    const setView = useCallback(
      (view: "chat" | "workflow") => {
        onViewChange?.(view);
        if (!isControlledView) {
          setIsWorkflowView(view === "workflow");
        }
      },
      [onViewChange, isControlledView]
    );

    const addFiles = useCallback(
      (files: File[]) => {
        if (!attachmentsSupported || files.length === 0) return;

        if (!pendingThreadIdRef.current && !threadId) {
          // randomId(), not crypto.randomUUID: the ALB serves plain HTTP, which
          // is not a secure context.
          pendingThreadIdRef.current = randomId();
        }
        const targetThread = threadId ?? pendingThreadIdRef.current!;

        setAttachments((prev) => {
          const room = MAX_FILES - prev.length;
          const accepted = files.slice(0, Math.max(0, room));

          const staged: PendingAttachment[] = accepted.map((file) => {
            const localId = randomId();
            const tooBig = file.size > MAX_BYTES;
            const wrongType = !isAllowed(file);
            const previewUrl = file.type.startsWith("image/")
              ? URL.createObjectURL(file)
              : undefined;

            if (tooBig || wrongType) {
              return {
                localId,
                file,
                state: "error",
                error: tooBig
                  ? `${file.name} is larger than 4.5 MB`
                  : `${file.name} is not a supported file type`,
                previewUrl,
              };
            }
            return { localId, file, state: "uploading", previewUrl };
          });

          staged
            .filter((item) => item.state === "uploading")
            .forEach((item) => {
              uploadAttachment(targetThread, item.file)
                .then((ref) =>
                  setAttachments((current) =>
                    current.map((entry) =>
                      entry.localId === item.localId
                        ? { ...entry, state: "ready", ref }
                        : entry
                    )
                  )
                )
                .catch((error: Error) =>
                  setAttachments((current) =>
                    current.map((entry) =>
                      entry.localId === item.localId
                        ? { ...entry, state: "error", error: error.message }
                        : entry
                    )
                  )
                );
            });

          return [...prev, ...staged];
        });
      },
      [attachmentsSupported, threadId]
    );

    const removeAttachment = useCallback((localId: string) => {
      setAttachments((prev) => {
        const target = prev.find((item) => item.localId === localId);
        if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
        return prev.filter((item) => item.localId !== localId);
      });
    }, []);

    const uploading = attachments.some((item) => item.state === "uploading");
    const readyRefs = attachments
      .filter((item) => item.state === "ready" && item.ref)
      .map((item) => item.ref!) as AttachmentRef[];

    const [input, _setInput] = useState("");
    const { scrollRef, contentRef } = useStickToBottom();

    const inputCallbackRef = useRef(onInput);
    inputCallbackRef.current = onInput;

    const setInput = useCallback(
      (value: string) => {
        _setInput(value);
        inputCallbackRef.current?.(value);
      },
      [inputCallbackRef]
    );

    const {
      stream,
      messages,
      todos,
      files,
      mcpApps,
      artifacts,
      setFiles,
      isLoading,
      isThreadLoading,
      error,
      status,
      interrupt,
      sendMessage,
      setAppModelContext,
      continueStream,
      stopStream,
    } = useChatContext();

    // MCP App → 대화. 규격 SHOULD: `ui/message` 는 대화에 사용자 메시지로 들어간다.
    // 응답이 진행 중이면 거부한다 — 진행 중 제출은 스트림을 끊고 새 턴을 시작하므로
    // 사용자가 버튼을 눌렀다는 이유로 그 결정을 대신할 수 없다. 앱은 isError 로 안다.
    const handleAppMessage = useCallback(
      (text: string): boolean => {
        if (isLoading || readOnly || !text.trim()) return false;
        sendMessage(text);
        return true;
      },
      [isLoading, readOnly, sendMessage],
    );
    const handleAppModelContext = useCallback(
      (context: Record<string, unknown>) => setAppModelContext(context),
      [setAppModelContext],
    );

    // readOnly blocks the send outright: the server would refuse this thread's
    // turn with a 409, so accepting the message would only lose it.
    const submitDisabled = isLoading || !!readOnly;

    const handleSubmit = useCallback(
      (e?: FormEvent) => {
        if (e) {
          e.preventDefault();
        }
        if (submitDisabled) return;

        // Read value from textarea ref to get the most up-to-date value
        // This ensures we capture the latest input even if state hasn't synced
        const currentValue = textareaRef.current?.value ?? input;
        const messageText = currentValue.trim();
        // An attachment alone is a valid message; the server synthesises prompt
        // text for it, because Bedrock rejects a document block without text.
        if ((!messageText && readyRefs.length === 0) || isLoading) return;
        if (uploading) return;

        // Set clearing flag to prevent onChange from restoring the value
        isClearingRef.current = true;

        // Clear input immediately - clear state first, then DOM as backup
        setInput("");
        // Also clear the textarea directly to ensure it's cleared immediately
        if (textareaRef.current) {
          textareaRef.current.value = "";
        }

        // Reset clearing flag after a brief delay to allow any pending events to complete
        setTimeout(() => {
          isClearingRef.current = false;
        }, 100);

        // sendMessage is now async, but we don't need to await it
        sendMessage(messageText, readyRefs.length > 0 ? readyRefs : undefined);

        attachments.forEach((item) => {
          if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        });
        setAttachments([]);
        pendingThreadIdRef.current = null;
      },
      [input, isLoading, sendMessage, setInput, submitDisabled, readyRefs, uploading, attachments]
    );

    const handleKeyDown = useCallback(
      (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (submitDisabled) return;
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          handleSubmit();
        }
      },
      [handleSubmit, submitDisabled]
    );

    const handleContinue = useCallback(() => {
      const preparingToCallTaskTool = isPreparingToCallTaskTool(messages);
      continueStream(preparingToCallTaskTool);
    }, [continueStream, messages]);

    const handleRestartFromAIMessage = useCallback(
      (message: Message) => {
        // Debug mode disabled - function not available
      },
      []
    );

    const handleRestartFromSubTask = useCallback(
      (toolCallId: string) => {
        // Debug mode disabled - function not available
      },
      []
    );

    // Reserved: additional UI state
    // TODO: can we make this part of the hook?
    const processedMessages = useMemo(() => {
      /*
     1. Loop through all messages
     2. For each AI message, add the AI message, and any tool calls to the messageMap
     3. For each tool message, find the corresponding tool call in the messageMap and update the status and output
    */
      const messageMap = new Map<
        string,
        { message: Message; toolCalls: ToolCall[] }
      >();
      messages.forEach((message: Message) => {
        if (message.type === "ai") {
          // Check if we already have processed tool calls for this message
          const existingData = messageMap.get(message.id!);
          const existingToolCalls = existingData?.toolCalls || [];
          
          const toolCallsInMessage: Array<{
            id?: string;
            function?: { name?: string; arguments?: unknown };
            name?: string;
            type?: string;
            args?: unknown;
            input?: unknown;
            status?: "pending" | "completed" | "error" | "interrupted";
          }> = [];
          if (
            message.additional_kwargs?.tool_calls &&
            Array.isArray(message.additional_kwargs.tool_calls)
          ) {
            toolCallsInMessage.push(...message.additional_kwargs.tool_calls);
          } else if (message.tool_calls && Array.isArray(message.tool_calls)) {
            toolCallsInMessage.push(
              ...message.tool_calls.filter(
                (toolCall: { name?: string }) => toolCall.name !== ""
              )
            );
          } else if (Array.isArray(message.content)) {
            const toolUseBlocks = message.content.filter(
              (block): block is MessageContent =>
                typeof block === "object" &&
                block !== null &&
                block.type === "tool_use"
            );
            toolCallsInMessage.push(...toolUseBlocks);
          }
          
          const toolCallsWithStatus = toolCallsInMessage.map(
            (toolCall: {
              id?: string;
              function?: { name?: string; arguments?: unknown };
              name?: string;
              type?: string;
              args?: unknown;
              input?: unknown;
              status?: "pending" | "completed" | "error" | "interrupted";
            }) => {
              const name =
                toolCall.function?.name ||
                toolCall.name ||
                toolCall.type ||
                "unknown";
              const args =
                toolCall.function?.arguments ||
                toolCall.args ||
                toolCall.input ||
                {};
              
              // Try to find existing tool call to preserve status and result
              const toolCallId = toolCall.id || `tool-${Math.random()}`;
              const existingToolCall = existingToolCalls.find(
                (tc: ToolCall) => tc.id === toolCallId
              );
              
              // Preserve existing status if available, otherwise use status from toolCall or set to pending
              const existingStatus = existingToolCall?.status || toolCall.status;
              const defaultStatus = interrupt ? "interrupted" : "pending";
              
              // Get result from toolCall (updated by useStream) or existingToolCall
              const result = (toolCall as any).result || existingToolCall?.result;
              // Where this call sat in the answer text. Preserved rather than
              // recomputed: only the moment the call opened knows it, and that
              // moment is long past by the time this runs.
              const contentOffset =
                (toolCall as any).contentOffset ?? existingToolCall?.contentOffset;

              return {
                id: toolCallId,
                name,
                args,
                status: existingStatus || defaultStatus,
                result: result,
                contentOffset,
              } as ToolCall;
            }
          );
          messageMap.set(message.id!, {
            message,
            toolCalls: toolCallsWithStatus,
          });
        } else if (message.type === "tool") {
          const toolCallId = message.tool_call_id;
          if (!toolCallId) {
            return;
          }
          for (const [, data] of messageMap.entries()) {
            const toolCallIndex = data.toolCalls.findIndex(
              (tc: ToolCall) => tc.id === toolCallId
            );
            if (toolCallIndex === -1) {
              continue;
            }
            data.toolCalls[toolCallIndex] = {
              ...data.toolCalls[toolCallIndex],
              status: "completed" as const,
              result: extractStringFromMessageContent(message),
            };
            break;
          }
        } else if (message.type === "human") {
          messageMap.set(message.id!, {
            message,
            toolCalls: [],
          });
        }
      });
      const processedArray = Array.from(messageMap.values());
      
      // Determine tool call completion status based on message sequence
      // If a message with tool calls is followed by another message, the tool calls are completed
      for (let i = 0; i < processedArray.length; i++) {
        const currentData = processedArray[i];
        const nextData = processedArray[i + 1];
        
        // If this message has tool calls and there's a next message, mark tool calls as completed
        if (currentData.toolCalls.length > 0 && nextData) {
          currentData.toolCalls = currentData.toolCalls.map((tc) => {
            // Only update if status is pending (preserve completed/error/interrupted from tool messages)
            if (tc.status === "pending") {
              return {
                ...tc,
                status: "completed" as const,
              };
            }
            return tc;
          });
        }
      }
      
      return processedArray.map((data, index) => {
        const prevMessage =
          index > 0 ? processedArray[index - 1].message : null;
        return {
          ...data,
          showAvatar: data.message.type !== prevMessage?.type,
        };
      });
    }, [messages, interrupt]);

    // One bucket per message. Done in a pass rather than per message because the
    // placement of a file with no `messageId` depends on the whole list — see
    // messageArtifacts.mjs for why such files exist and why dropping them is not
    // a neutral outcome.
    const artifactsByMessage = useMemo(
      () =>
        assignArtifactsToMessages(
          processedMessages.map((data) => ({
            id: data.message.id,
            type: data.message.type,
            toolCallIds: data.toolCalls.map((tc: ToolCall) => tc.id),
          })),
          artifacts ?? []
        ),
      [processedMessages, artifacts]
    );

    const toggle = !hideInternalToggle && (
      <div className="flex w-full justify-center">
        <div className="flex h-6 w-[134px] items-center gap-0 overflow-hidden rounded border border-border bg-card p-[3px] text-xs">
          <button
            type="button"
            onClick={() => setView("chat")}
            className={cn(
              "flex h-full flex-1 items-center justify-center truncate rounded p-[3px]",
              { "bg-[#F4F3FF]": !workflowView }
            )}
          >
            Chat
          </button>
          <button
            type="button"
            onClick={() => setView("workflow")}
            className={cn(
              "flex h-full flex-1 items-center justify-center truncate rounded p-[3px]",
              { "bg-[#F4F3FF]": workflowView }
            )}
          >
            Workflow
          </button>
        </div>
      </div>
    );

    if (isWorkflowView) {
      return (
        <div className="flex h-full w-full flex-col font-sans">
          {toggle}
          <div className="flex flex-1 overflow-hidden">
            <div className="flex flex-1 flex-col overflow-hidden">
              {isThreadLoading && (
                <div className="absolute left-0 top-0 z-10 flex h-full w-full justify-center pt-[100px]">
                  <LoaderCircle className="size-8 animate-spin text-primary" />
                </div>
              )}
              <div className="flex-1 overflow-y-auto px-6 pb-4 pt-4">
                <div className="flex h-full w-full items-stretch">
                  <div className="flex h-full w-full flex-1">
                    {/* <AgentGraphVisualization
                      configurable={
                        (getMessagesMetadata(messages[messages.length - 1])
                          ?.activeAssistant?.config?.configurable as any) || {}
                      }
                      name={
                        getMessagesMetadata(messages[messages.length - 1])
                          ?.activeAssistant?.name || "Agent"
                      }
                    /> */}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      );
    }

    const groupedTodos = {
      in_progress: todos.filter((t) => t.status === "in_progress"),
      pending: todos.filter((t) => t.status === "pending"),
      completed: todos.filter((t) => t.status === "completed"),
    };

    const hasTasks = todos.length > 0;
    const hasFiles = Object.keys(files).length > 0;

    const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      // Ignore input changes while we're clearing the input
      if (isClearingRef.current) {
        return;
      }
      setInput(e.target.value);
    };

    return (
      <div className="flex flex-1 flex-col overflow-hidden relative">
        <div
          className="flex-1 overflow-y-auto overflow-x-hidden overscroll-contain"
          ref={scrollRef}
        >
          <div
            className="mx-auto w-full max-w-[52rem] px-4 pb-5 pt-4"
            ref={contentRef}
          >
            {isThreadLoading ? (
              skeleton
            ) : (
              <>
                {processedMessages.map((data, index) => {
                  // Filter UI resources by message_id OR by tool_call_id
                  const toolCallIds = data.toolCalls.map((tc: any) => tc.id);
                  const matchesMessage = (meta: any) => {
                    if (meta?.message_id === data.message.id) {
                      return true;
                    }
                    if (meta?.tool_call_id && toolCallIds.includes(meta.tool_call_id)) {
                      return true;
                    }
                    return false;
                  };
                  const messageMcpApps = mcpApps?.filter((m: any) => matchesMessage({
                    message_id: m.messageId,
                    tool_call_id: m.toolCallId,
                  }));
                  const messageArtifacts = artifactsByMessage[index];

                  return (
                    <ChatMessage
                      key={data.message.id}
                      message={data.message}
                      toolCalls={data.toolCalls}
                      onRestartFromAIMessage={handleRestartFromAIMessage}
                      onRestartFromSubTask={handleRestartFromSubTask}
                      isLoading={isLoading}
                      isLastMessage={index === processedMessages.length - 1}
                      interrupt={interrupt}
                      mcpApps={messageMcpApps}
                      onAppMessage={handleAppMessage}
                      onAppModelContext={handleAppModelContext}
                      artifacts={messageArtifacts}
                      activeArtifact={activeArtifact}
                      onOpenArtifact={onOpenArtifact}
                      stream={stream}
                    />
                  );
                })}
                {/* Sits where the answer will appear, after the message just
                    sent, so the wait is explained at the spot the user is already
                    looking. Replaced by the reply as soon as the first token
                    lands — the stream hook clears `status` on real content. */}
                {status && (
                  <AgentStatusLine
                    label={status.label}
                    phase={status.phase}
                    startedAt={status.startedAt}
                    className="mt-2"
                  />
                )}
                {/* Not shown when read-only: Continue submits a run like any
                    other, so on a thread that cannot be continued it would just
                    collect a 409. The composer is disabled for the same reason,
                    and this is the one other way to start a turn. */}
                {interrupt && !readOnly && (
                  <div className="mt-4">
                    <Button
                      onClick={handleContinue}
                      variant="outline"
                      className="rounded-full px-3 py-1 text-xs"
                    >
                      Continue
                    </Button>
                  </div>
                )}
                {/* A run refused before it produced anything has no in-band
                    error event to render, so without this the send button just
                    stops spinning. The 409 from a thread/agent mismatch lands
                    here. Rendered in the message flow — the error answers the
                    message the user just sent, so it reads as part of the
                    conversation rather than a composer state. */}
                {error && (
                  <div className="mt-4">
                    <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                      {error}
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        <div className="flex-shrink-0 bg-background">
          <div
            className={cn(
              "mx-4 mb-4 flex flex-shrink-0 flex-col overflow-hidden rounded-lg border border-border bg-card shadow-sm",
              // The whole composer lights up on focus, so the caret is never
              // the only thing telling you where input goes.
              "focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-ring/20",
              "mx-auto w-[calc(100%-32px)] max-w-[52rem] transition-[border-color,box-shadow] duration-150 ease-snap"
            )}
          >
            {(hasTasks || hasFiles) && (
              <div className="flex max-h-72 flex-col overflow-y-auto border-b border-border bg-sidebar empty:hidden">
                {!metaOpen && (
                  <>
                    {(() => {
                      const activeTask = todos.find(
                        (t) => t.status === "in_progress"
                      );

                      const totalTasks = todos.length;
                      const remainingTasks =
                        totalTasks - groupedTodos.pending.length;
                      const isCompleted = totalTasks === remainingTasks;

                      const tasksTrigger = (() => {
                        if (!hasTasks) return null;
                        return (
                          <button
                            type="button"
                            onClick={() =>
                              setMetaOpen((prev) =>
                                prev === "tasks" ? null : "tasks"
                              )
                            }
                            className="grid w-full cursor-pointer grid-cols-[auto_auto_1fr] items-center gap-3 px-[18px] py-3 text-left"
                            aria-expanded={metaOpen === "tasks"}
                          >
                            {(() => {
                              if (isCompleted) {
                                return [
                                  <CheckCircle
                                    key="icon"
                                    size={16}
                                    className="text-success"
                                  />,
                                  <span
                                    key="label"
                                    className="ml-[1px] min-w-0 truncate text-sm"
                                  >
                                    All tasks completed
                                  </span>,
                                ];
                              }

                              if (activeTask != null) {
                                return [
                                  <div key="icon">
                                    {getStatusIcon(activeTask.status)}
                                  </div>,
                                  <span
                                    key="label"
                                    className="ml-[1px] min-w-0 truncate text-sm"
                                  >
                                    Task{" "}
                                    {totalTasks - groupedTodos.pending.length}{" "}
                                    of {totalTasks}
                                  </span>,
                                  <span
                                    key="content"
                                    className="min-w-0 gap-2 truncate text-sm text-muted-foreground"
                                  >
                                    {activeTask.content}
                                  </span>,
                                ];
                              }

                              return [
                                <Circle
                                  key="icon"
                                  size={16}
                                  className="text-muted-foreground"
                                />,
                                <span
                                  key="label"
                                  className="ml-[1px] min-w-0 truncate text-sm"
                                >
                                  Task{" "}
                                  {totalTasks - groupedTodos.pending.length} of{" "}
                                  {totalTasks}
                                </span>,
                              ];
                            })()}
                          </button>
                        );
                      })();

                      const filesTrigger = (() => {
                        if (!hasFiles) return null;
                        return (
                          <button
                            type="button"
                            onClick={() =>
                              setMetaOpen((prev) =>
                                prev === "files" ? null : "files"
                              )
                            }
                            className="flex flex-shrink-0 cursor-pointer items-center gap-2 px-[18px] py-3 text-left text-sm"
                            aria-expanded={metaOpen === "files"}
                          >
                            <FileIcon size={16} />
                            Files (State)
                            <span className="h-4 min-w-4 rounded-full bg-primary px-0.5 text-center text-[10px] leading-4 text-primary-foreground">
                              {Object.keys(files).length}
                            </span>
                          </button>
                        );
                      })();

                      return (
                        <div className="grid grid-cols-[1fr_auto_auto] items-center">
                          {tasksTrigger}
                          {filesTrigger}
                        </div>
                      );
                    })()}
                  </>
                )}

                {metaOpen && (
                  <>
                    <div className="sticky top-0 flex items-stretch bg-sidebar text-sm">
                      {hasTasks && (
                        <button
                          type="button"
                          className="py-3 pr-4 first:pl-[18px] aria-expanded:font-semibold"
                          onClick={() =>
                            setMetaOpen((prev) =>
                              prev === "tasks" ? null : "tasks"
                            )
                          }
                          aria-expanded={metaOpen === "tasks"}
                        >
                          Tasks
                        </button>
                      )}
                      {hasFiles && (
                        <button
                          type="button"
                          className="inline-flex items-center gap-2 py-3 pr-4 first:pl-[18px] aria-expanded:font-semibold"
                          onClick={() =>
                            setMetaOpen((prev) =>
                              prev === "files" ? null : "files"
                            )
                          }
                          aria-expanded={metaOpen === "files"}
                        >
                          Files (State)
                          <span className="h-4 min-w-4 rounded-full bg-primary px-0.5 text-center text-[10px] leading-4 text-primary-foreground">
                            {Object.keys(files).length}
                          </span>
                        </button>
                      )}
                      <button
                        aria-label="Close"
                        className="flex-1"
                        onClick={() => setMetaOpen(null)}
                      />
                    </div>
                    <div
                      ref={tasksContainerRef}
                      className="px-[18px]"
                    >
                      {metaOpen === "tasks" &&
                        Object.entries(groupedTodos)
                          .filter(([_, todos]) => todos.length > 0)
                          .map(([status, todos]) => (
                            <div className="mb-4">
                              <h3 className="mb-1 caps-label-xs text-muted-foreground">
                                {
                                  {
                                    pending: "Pending",
                                    in_progress: "In Progress",
                                    completed: "Completed",
                                  }[status]
                                }
                              </h3>
                              <div className="grid grid-cols-[auto_1fr] gap-3 rounded-sm p-1 pl-0 text-sm">
                                {todos.map((todo, index) => (
                                  <Fragment
                                    key={`${status}_${todo.id}_${index}`}
                                  >
                                    {getStatusIcon(todo.status, "mt-0.5")}
                                    <span className="break-words text-inherit">
                                      {todo.content}
                                    </span>
                                  </Fragment>
                                ))}
                              </div>
                            </div>
                          ))}

                      {metaOpen === "files" && (
                        <div className="mb-6">
                          <FilesPopover
                            files={files}
                            setFiles={setFiles}
                            editDisabled={
                              isLoading === true || interrupt !== undefined
                            }
                          />
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}
            {/* Above the composer, not below it: this explains why the input
                beneath is disabled, and an explanation that follows the thing it
                disables reads as an unrelated footer. */}
            {banner && <div className="mb-2">{banner}</div>}
            <form
              onSubmit={handleSubmit}
              className={cn(
                "flex flex-col",
                dragDepth > 0 && "ring-2 ring-[#2F6868]"
              )}
              onDragEnter={(e) => {
                if (!attachmentsSupported) return;
                e.preventDefault();
                setDragDepth((d) => d + 1);
              }}
              onDragOver={(e) => e.preventDefault()}
              onDragLeave={() => setDragDepth((d) => Math.max(0, d - 1))}
              onDrop={(e) => {
                e.preventDefault();
                setDragDepth(0);
                addFiles(Array.from(e.dataTransfer.files));
              }}
            >
              <AttachmentBar items={attachments} onRemove={removeAttachment} />
              <textarea
                ref={textareaRef}
                value={input}
                onChange={handleInputChange}
                onKeyDown={handleKeyDown}
                onPaste={(e) => {
                  const files = Array.from(e.clipboardData.files);
                  if (files.length > 0) {
                    e.preventDefault();
                    addFiles(files);
                  }
                }}
                disabled={readOnly}
                placeholder={
                  readOnly
                    ? "이 대화는 이어갈 수 없습니다"
                    : isLoading
                      ? "Running..."
                      : "Write your message..."
                }
                className="font-inherit field-sizing-content max-h-56 flex-1 resize-none border-0 bg-transparent px-3.5 pb-2 pt-3 text-sm leading-relaxed text-foreground outline-none placeholder:text-muted-foreground/70 disabled:cursor-not-allowed"
                rows={1}
              />
              <div className="flex items-center justify-between gap-3 px-2.5 pb-2.5 pt-0.5">
                <div className="flex min-w-0 items-center gap-2">{controls}</div>

                <div className="flex flex-shrink-0 items-center gap-1.5">
                  {/* Harness agents only; renders nothing for a runtime. */}
                  <ThreadOverrides />
                  <AgentSwitcher />
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept={ACCEPT_ATTRIBUTE}
                    className="hidden"
                    onChange={(e) => {
                      addFiles(Array.from(e.target.files ?? []));
                      e.target.value = "";
                    }}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    disabled={!attachmentsSupported || attachments.length >= MAX_FILES}
                    onClick={() => fileInputRef.current?.click()}
                    title={
                      attachmentsSupported
                        ? "Attach a file"
                        : "This agent does not support attachments"
                    }
                    aria-label="Attach a file"
                  >
                    <Paperclip size={18} />
                  </Button>
                  <Button
                    size="sm"
                    type={isLoading ? "button" : "submit"}
                    variant={isLoading ? "destructive" : "default"}
                    onClick={isLoading ? stopStream : handleSubmit}
                    disabled={
                      !isLoading &&
                      (submitDisabled ||
                        uploading ||
                        (!input.trim() && readyRefs.length === 0))
                    }
                  >
                    {isLoading ? (
                      <>
                        <Square size={13} />
                        <span>Stop</span>
                      </>
                    ) : (
                      <>
                        <ArrowUp size={15} />
                        <span>Send</span>
                      </>
                    )}
                  </Button>
                </div>
              </div>
            </form>
          </div>
        </div>
      </div>
    );
  }
);

ChatInterface.displayName = "ChatInterface";
