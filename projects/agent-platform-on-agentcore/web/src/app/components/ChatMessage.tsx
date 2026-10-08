"use client";

import React, { useMemo, useState, useCallback } from "react";
import { useQueryState } from "nuqs";
import { SubAgentIndicator } from "@/app/components/SubAgentIndicator";
import { ToolCallBox } from "@/app/components/ToolCallBox";
import { MarkdownContent } from "@/app/components/MarkdownContent";
import { McpAppView } from "@/app/components/McpAppView";
import { ArtifactCard } from "@/app/components/ArtifactCard";
import { MessageAttachment } from "@/app/components/MessageAttachment";
import { ChartRenderer } from "@/app/components/ChartRenderer";
import { VerificationNote } from "@/app/components/VerificationNote";
import type { ArtifactEvent } from "@/lib/artifacts";
import type {
  Chart,
  SubAgent,
  ToolCall,
  Verification,
} from "@/app/types/types";
import type { Interrupt, Message } from "@/lib/api-types";
import {
  buildTurnTimeline,
  extractSubAgentContent,
  extractStringFromMessageContent,
  getInterruptTitle,
} from "@/app/utils/utils";
import { cn } from "@/lib/utils";
import type { AttachmentRef } from "@/lib/attachments";

interface ChatMessageProps {
  message: Message;
  toolCalls: ToolCall[];
  onRestartFromAIMessage: (message: Message) => void;
  onRestartFromSubTask: (toolCallId: string) => void;
  isLastMessage?: boolean;
  isLoading?: boolean;
  interrupt?: Interrupt;
  mcpApps?: any[];
  /** MCP App 의 `ui/message`. true 를 돌려주면 대화에 들어갔다는 뜻이다. */
  onAppMessage?: (text: string) => boolean;
  /** MCP App 의 `ui/update-model-context`. 다음 턴에 모델에게 간다. */
  onAppModelContext?: (context: Record<string, unknown>) => void;
  artifacts?: ArtifactEvent[];
  activeArtifact?: { artifactId: string; version: number } | null;
  onOpenArtifact?: (artifactId: string, version: number) => void;
  stream?: any;
}

export const ChatMessage = React.memo<ChatMessageProps>(
  ({
    message,
    toolCalls,
    onRestartFromAIMessage,
    onRestartFromSubTask,
    isLastMessage,
    isLoading,
    interrupt,
    mcpApps,
    onAppMessage,
    onAppModelContext,
    artifacts,
    activeArtifact,
    onOpenArtifact,
    stream,
  }) => {
    const isUser = message.type === "human";
    const isAIMessage = message.type === "ai";
    const messageContent = extractStringFromMessageContent(message);
    const hasReasoning = message.reasoning && message.reasoning.trim() !== "";
    // Carried on the message rather than passed in: they belong to the turn that
    // produced them, and a reopened thread has to render them from the stored
    // record with no live stream to consult.
    const charts = ((message as any).charts ?? []) as Chart[];
    const verifications = ((message as any).verifications ??
      []) as Verification[];

    const [threadIdParam] = useQueryState("threadId");
    // The message's own attachment references. extractStringFromMessageContent
    // keeps only text blocks — correctly — so the strip reads these directly.
    const attachmentRefs = useMemo<AttachmentRef[]>(() => {
      if (!Array.isArray(message.content)) return [];
      return message.content.filter(
        (block): block is AttachmentRef =>
          typeof block === "object" &&
          block !== null &&
          (block as { type?: string }).type === "attachment"
      );
    }, [message.content]);

    // Per-message reasoning collapse/expand state
    const [localShowReasoning, setLocalShowReasoning] = useState(true);
    const subAgents = useMemo(() => {
      return toolCalls
        .filter((toolCall: ToolCall) => {
          return (
            toolCall.name === "task" &&
            toolCall.args["subagent_type"] &&
            toolCall.args["subagent_type"] !== "" &&
            toolCall.args["subagent_type"] !== null
          );
        })
        .map((toolCall: ToolCall) => {
          return {
            id: toolCall.id,
            name: toolCall.name,
            subAgentName: String(toolCall.args["subagent_type"] || ""),
            input: toolCall.args,
            output: toolCall.result ? { result: toolCall.result } : undefined,
            status: toolCall.status,
          } as SubAgent;
        });
    }, [toolCalls]);

    const [expandedSubAgents, setExpandedSubAgents] = useState<
      Record<string, boolean>
    >({});
    const isSubAgentExpanded = useCallback(
      (id: string) => expandedSubAgents[id] ?? true,
      [expandedSubAgents]
    );
    const toggleSubAgent = useCallback((id: string) => {
      setExpandedSubAgents((prev) => ({
        ...prev,
        [id]: prev[id] === undefined ? false : !prev[id],
      }));
    }, []);

    const interruptTitle = interrupt ? getInterruptTitle(interrupt) : "";

    // Text and tool calls in the order they actually happened. See
    // buildTurnTimeline: a whole turn shares one message, so the two must be
    // reassembled from each call's recorded position in the text.
    const timeline = useMemo(
      () => buildTurnTimeline(messageContent, toolCalls),
      [messageContent, toolCalls]
    );

    /* An interrupt can only be waiting on the turn's final call. Identified by id
       rather than array position, since the timeline reorders the calls. */
    const lastToolCallId = useMemo(() => {
      const visible = toolCalls.filter((tc) => tc.name !== "task");
      return visible.length > 0 ? visible[visible.length - 1].id : null;
    }, [toolCalls]);

    /* The streaming caret belongs on the last text segment only — putting it on
       every one would blink mid-answer above a finished tool call. */
    const lastTextKey = useMemo(() => {
      for (let i = timeline.length - 1; i >= 0; i--) {
        if (timeline[i].kind === "text") return timeline[i].key;
      }
      return null;
    }, [timeline]);

    return (
      <div
        className={cn(
          "flex w-full max-w-full overflow-x-hidden",
          isUser && "flex-row-reverse"
        )}
      >
        <div
          className={cn(
            "min-w-0 max-w-full",
            isUser ? "max-w-[70%]" : "w-full"
          )}
        >
          {attachmentRefs.length > 0 && threadIdParam && (
            <div
              className={cn(
                "mt-4 flex flex-wrap gap-2",
                isUser && "justify-end"
              )}
            >
              {attachmentRefs.map((ref) => (
                <MessageAttachment
                  key={ref.attachment_id}
                  threadId={threadIdParam}
                  attachment={ref}
                />
              ))}
            </div>
          )}
          {hasReasoning && isAIMessage && (
            <div className="mt-4 mb-2">
              <button
                onClick={() => setLocalShowReasoning(!localShowReasoning)}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
              >
                <span>{localShowReasoning ? "▼" : "▶"}</span>
                <span>Reasoning {localShowReasoning ? "(hide)" : "(show)"}</span>
              </button>
              {localShowReasoning && (
                <div className="mt-2 rounded-md border border-border bg-muted/50 p-2.5 text-xs leading-normal text-muted-foreground">
                  <MarkdownContent content={message.reasoning || ""} />
                </div>
              )}
            </div>
          )}
          {/* Text and tool calls in the order they actually happened, so the
              answer never appears above the calls that produced it. */}
          {timeline.map((part) =>
            part.kind === "text" ? (
              <div
                key={part.key}
                className={cn("relative flex items-end gap-0")}
              >
                <div
                  className={cn(
                    "mt-4 overflow-hidden break-words text-sm font-normal leading-[150%]",
                    isUser
                      ? "rounded-lg rounded-br-sm border border-border bg-primary-tint px-3 py-2 text-foreground"
                      : "text-foreground"
                  )}
                >
                  {isUser ? (
                    <p className="m-0 whitespace-pre-wrap break-words text-sm leading-relaxed">
                      {part.text}
                    </p>
                  ) : (
                    <MarkdownContent
                      content={part.text}
                      isStreaming={
                        isLastMessage && isLoading && part.key === lastTextKey
                      }
                    />
                  )}
                </div>
              </div>
            ) : (
              <div
                key={part.key}
                className="mt-4 flex w-full flex-col gap-1.5"
              >
                {part.calls.map((toolCall: ToolCall) => (
                  <ToolCallBox
                    key={toolCall.id}
                    toolCall={toolCall}
                    stream={stream}
                    // Lets a browser screenshot be re-fetched once its presigned
                    // URL has expired; see browserScreenshotUrl.
                    threadId={threadIdParam ?? undefined}
                    isInterrupted={
                      isLastMessage &&
                      toolCall.name === interruptTitle &&
                      toolCall.id === lastToolCallId
                    }
                  />
                ))}
              </div>
            )
          )}
          {!isUser && subAgents.length > 0 && (
            <div className="flex w-fit max-w-full flex-col gap-4">
              {subAgents.map((subAgent) => (
                <div
                  key={subAgent.id}
                  className="flex w-full flex-col gap-2"
                >
                  <div className="flex items-end gap-2">
                    <div className="w-[calc(100%-100px)]">
                      <SubAgentIndicator
                        subAgent={subAgent}
                        onClick={() => toggleSubAgent(subAgent.id)}
                        isExpanded={isSubAgentExpanded(subAgent.id)}
                      />
                    </div>
                    <div className="relative h-full min-h-[40px] w-[72px] flex-shrink-0">
                    </div>
                  </div>
                  {isSubAgentExpanded(subAgent.id) && (
                    <div className="w-full max-w-full">
                      <div className="rounded-md border border-border bg-muted/50 p-3">
                        <h4 className="mb-1.5 caps-label-xs text-muted-foreground">
                          Input
                        </h4>
                        <div className="mb-4">
                          <MarkdownContent
                            content={extractSubAgentContent(subAgent.input)}
                          />
                        </div>
                        {subAgent.output && (
                          <>
                            <h4 className="mb-1.5 caps-label-xs text-muted-foreground">
                              Output
                            </h4>
                            <MarkdownContent
                              content={extractSubAgentContent(subAgent.output)}
                            />
                          </>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {!isUser && charts.length > 0 && (
            <div className="mt-4 flex flex-col gap-3">
              {charts.map((chart, idx) => (
                <ChartRenderer key={idx} chart={chart} />
              ))}
            </div>
          )}
          {!isUser && verifications.length > 0 && (
            <div className="mt-3 flex flex-col gap-2">
              {verifications.map((verification, idx) => (
                <VerificationNote key={idx} verification={verification} />
              ))}
            </div>
          )}
          {!isUser && (
            <>
              {artifacts && artifacts.length > 0 && onOpenArtifact && (
                <div className="mt-4 flex flex-col gap-2">
                  {artifacts.map((artifact) => (
                    <ArtifactCard
                      key={`${artifact.artifactId}-${artifact.version}`}
                      artifact={artifact}
                      isActive={
                        activeArtifact?.artifactId === artifact.artifactId &&
                        activeArtifact?.version === artifact.version
                      }
                      onOpen={onOpenArtifact}
                    />
                  ))}
                </div>
              )}
              {mcpApps && mcpApps.length > 0 && (
                <div className="mt-4 space-y-4">
                  {mcpApps.map((app, idx) => {
                    // The app is a view onto this tool call, so it needs the call's
                    // input and result. Without them the view never receives
                    // ui/notifications/tool-result and its controls stay disabled —
                    // the app renders but does nothing.
                    const call = toolCalls.find(
                      (tc: ToolCall) => tc.id === app.toolCallId
                    );
                    const cancelled = (
                      (stream?.values?.cancelledToolCalls as string[] | undefined) ?? []
                    ).includes(app.toolCallId);
                    return (
                    <McpAppView
                      key={`${app.toolCallId}-${idx}`}
                      recordId={app.recordId}
                      resourceUri={app.resourceUri}
                      toolName={app.toolName}
                      toolCallId={app.toolCallId}
                      toolInput={call?.args}
                      toolStatus={call?.status}
                      toolResult={call?.result}
                      toolCancelled={cancelled}
                      onAppMessage={onAppMessage}
                      onModelContext={onAppModelContext}
                    />
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    );
  }
);

ChatMessage.displayName = "ChatMessage";
