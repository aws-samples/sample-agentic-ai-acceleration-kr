/**
 * Custom SSE stream hook to replace LangGraph SDK useStream
 */
import { useState, useEffect, useCallback, useRef } from "react";
import { ApiClient, NoActiveRunError, RunInFlightError } from "@/lib/api-client";
import type { Message } from "@/lib/api-types";
import { randomId } from "@/lib/utils";
// The last-resort settle for tool calls, kept as a pure function so it can be
// tested without a renderer — see settleToolCalls.test.mjs.
import { settlePendingToolCalls } from "./settleToolCalls.mjs";
// Where a toolResult lands, likewise pure — see applyToolResult.test.mjs for the
// overwrite contract it shares with the server.
import { applyToolResult } from "./applyToolResult.mjs";
// Closing a tool-use block on contentBlockStop, likewise pure — see closeToolUse.test.mjs.
import { closeToolUse } from "./closeToolUse.mjs";
// The stored transcript minus the turn in flight, for reopening a busy thread —
// see resumeRun.test.mjs.
import { applyRunBaseline } from "./resumeRun.mjs";

export interface StreamState<T = any> {
  values: T;
  messages: Message[];
  isLoading: boolean;
  isThreadLoading: boolean;
  interrupt?: any;
  getMessagesMetadata?: () => any;
  /**
   * Why the last run failed before it produced anything, or undefined.
   *
   * A request refused outright — most notably a 409 from addressing a thread's
   * turn to the wrong agent — never reaches the SSE loop, so it has no in-band
   * error event to render. Without this the send button simply stops spinning
   * and nothing says why.
   */
  error?: string;
  /**
   * What the agent is doing right now, while it produces no text.
   *
   * A runtime that delegates to sub-agents is silent for tens of seconds before
   * its first token, which is indistinguishable from a hang. The runtime reports
   * progress meanwhile and this carries it to the UI.
   *
   * Transient by design: never persisted, and cleared the moment real content
   * arrives or the run ends. `startedAt` is a client clock reading so the status
   * line can tick a timer between updates, which arrive only every few seconds.
   */
  status?: {
    label: string;
    phase?: string;
    startedAt: number;
  };
}

export interface UseStreamOptions {
  threadId: string | null;
  apiClient: ApiClient;
  onThreadId?: (threadId: string) => void;
  onFinish?: () => void;
  onError?: (error: Error) => void;
  onCreated?: () => void;
}

export function useStream<T = any>(options: UseStreamOptions) {
  const {
    threadId,
    apiClient,
    onThreadId,
    onFinish,
    onError,
    onCreated,
  } = options;

  const [state, setState] = useState<StreamState<T>>({
    values: {} as T,
    messages: [],
    isLoading: false,
    isThreadLoading: false,
  });

  const abortControllerRef = useRef<AbortController | null>(null);
  const currentThreadIdRef = useRef<string | null>(threadId);
  /**
   * Which thread the state below was loaded for.
   *
   * Deliberately *not* seeded from `threadId`: the sync effect only loads when
   * this differs from the current thread, so seeding it would make a mount that
   * already has a threadId — landing on `/?threadId=…` from a link, a reload, or
   * the sidebar on another route — look already-loaded and render an empty chat.
   * `undefined` means "nothing loaded yet" and can never equal a real id.
   */
  const loadedThreadIdRef = useRef<string | null | undefined>(undefined);
  /**
   * The id an in-flight run invented for itself, until the sync effect has seen it.
   *
   * A run started from a new chat picks its own thread id and reports it upward on
   * `thread_created`, which puts it in the URL and so comes back as a *changed*
   * `threadId`. To the sync effect below that is indistinguishable from the user
   * clicking another thread — and it responded in kind: it aborted the very stream
   * that had just created the thread and refetched the turn from DynamoDB
   * mid-answer. The reply survived only because the server keeps writing it, but
   * every tool call froze at whatever the stored copy said.
   *
   * Deliberately not `currentThreadIdRef`, which is seeded from `threadId` and so
   * would also match on a mount that already has one — the case that genuinely
   * does need loading.
   */
  const adoptedThreadIdRef = useRef<string | null>(null);

  // Helper to add message directly to state
  const addMessage = useCallback((message: Message) => {
    setState((prev) => ({
      ...prev,
      messages: [...prev.messages, message],
    }));
  }, []);

  /**
   * Drive one SSE body into state. Shared by a fresh run and a re-attached one:
   * the frames are identical, so the reader is too. `targetThreadId` is the
   * thread the frames belong to; the loop stops when the UI has moved on.
   */
  const consume = useCallback(
    async (
      stream: ReadableStream<Uint8Array>,
      abortController: AbortController,
      targetThreadId: string
    ) => {
      const reader = stream.getReader();
      const decoder = new TextDecoder();
      // Holds the trailing partial line between reads. A `data:` line can be
      // split across chunk boundaries, and parsing it early throws and drops
      // the token.
      let buffer = "";
      let streamEnded = false;
      let currentMessageId: string | null = null; // Track current streaming message ID
      let currentToolUseId: string | null = null; // Track current tool use ID for Strands Agent SDK

      try {
        while (true) {
          if (abortController.signal.aborted) {
            break;
          }

          // Check if thread has changed - if so, abort this stream
          if (currentThreadIdRef.current !== targetThreadId && currentThreadIdRef.current !== null) {
            break;
          }

          const { done, value } = await reader.read();

          let lines: string[];
          if (done) {
            // Flush whatever the last chunk left behind before exiting.
            buffer += decoder.decode();
            lines = buffer.split("\n");
            buffer = "";
            streamEnded = true;
          } else {
            buffer += decoder.decode(value, { stream: true });
            lines = buffer.split("\n");
            // The final element is either "" (chunk ended on a newline) or an
            // incomplete line; either way it belongs to the next read.
            buffer = lines.pop() ?? "";
          }

          for (const line of lines) {
            // Check again before processing each event
            if (currentThreadIdRef.current !== targetThreadId && currentThreadIdRef.current !== null) {
              break;
            }
            
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                
                // Handle Strands Agent format: {"event": {"messageStart": {...}, "contentBlockDelta": {...}, ...}}
                // Also handle legacy format: {"event": "message_start", "data": {...}}
                let eventData: any = {};
                let messageId: string | null = null;
                
                if (data.event && typeof data.event === "object") {
                  // Strands Agent format
                  const eventObj = data.event;
                  
                  if (eventObj.messageStart) {
                    eventData = eventObj.messageStart;
                    messageId = eventData.id || null;
                    // If no ID in messageStart, generate one and use it consistently
                    if (!messageId) {
                      messageId = `msg-${Date.now()}`;
                    }
                    
                    // Before starting new message, mark all pending tool calls in previous message as completed
                    // This handles the case where tool execution completes and agent moves to next message
                    if (currentMessageId) {
                      setState((prev) => {
                        const existingMessages = prev.messages || [];
                        const newMessages = [...existingMessages];
                        
                        const prevIndex = newMessages.findIndex(
                          (m) => m.id === currentMessageId && m.type === "ai"
                        );
                        
                        if (prevIndex >= 0) {
                          const prevMsg = newMessages[prevIndex];
                          const prevToolCalls = (prevMsg as any).tool_calls || [];
                          
                          // Check if there are any pending tool calls to update
                          const hasPendingToolCalls = prevToolCalls.some((tc: any) => tc.status === "pending");
                          
                          if (hasPendingToolCalls) {
                            const updatedToolCalls = prevToolCalls.map((tc: any) => {
                              if (tc.status === "pending") {
                                // Parse tool input if it's a string
                                let parsedArgs = tc.args;
                                if (typeof parsedArgs === "string") {
                                  try {
                                    parsedArgs = JSON.parse(parsedArgs);
                                  } catch {
                                    // Keep as string if parsing fails
                                  }
                                }
                                return {
                                  ...tc,
                                  args: parsedArgs,
                                  status: "completed" as const,
                                };
                              }
                              return tc;
                            });
                            
                            newMessages[prevIndex] = {
                              ...prevMsg,
                              tool_calls: updatedToolCalls,
                            } as any;
                          }
                        }
                        
                        return {
                          ...prev,
                          messages: newMessages,
                        };
                      });
                    }
                    
                    currentMessageId = messageId;
                    
                    // Create empty message when messageStart is received
                    // messageId is guaranteed to be string at this point
                    const finalMessageId: string = messageId;
                    setState((prev) => {
                      const existingMessages = prev.messages || [];
                      const newMessages = [...existingMessages];
                      
                      // Check if message already exists
                      const index = newMessages.findIndex(
                        (m) => m.id === finalMessageId && m.type === "ai"
                      );
                      
                      if (index < 0) {
                        // Create new empty AI message
                        newMessages.push({
                          id: finalMessageId,
                          type: "ai",
                          content: "",
                        });
                      }
                      
                      return {
                        ...prev,
                        messages: newMessages,
                      };
                    });
                  } else if (eventObj.contentBlockStart) {
                    // Handle tool use start for Strands Agent SDK
                    eventData = eventObj.contentBlockStart;
                    const start = eventData.start || {};
                    const toolUse = start.toolUse;
                    
                    if (toolUse) {
                      const toolUseId = toolUse.toolUseId;
                      const toolName = toolUse.name;
                      const messageIdToUse: string = currentMessageId || `msg-${Date.now()}`;
                      if (!currentMessageId) {
                        currentMessageId = messageIdToUse;
                      }
                      
                      if (toolUseId && toolName) {
                        currentToolUseId = toolUseId;
                        // Add tool call to message
                        setState((prev) => {
                          const existingMessages = prev.messages || [];
                          const newMessages = [...existingMessages];
                          
                          const index = newMessages.findIndex(
                            (m) => m.id === messageIdToUse && m.type === "ai"
                          );
                          
                          if (index >= 0) {
                            const existingMsg = newMessages[index];
                            const existingToolCalls = (existingMsg as any).tool_calls || [];
                            // Check if tool call already exists
                            const toolCallExists = existingToolCalls.some(
                              (tc: any) => tc.id === toolUseId
                            );
                            if (!toolCallExists) {
                              // Create new array with immutable update
                              const newToolCalls = [
                                ...existingToolCalls,
                                {
                                  id: toolUseId,
                                  name: toolName,
                                  args: "",
                                  status: "pending" as const,
                                  // How much answer text preceded this call.
                                  // The whole turn shares one message, so this
                                  // is the only record of the order text and
                                  // calls actually arrived in.
                                  contentOffset:
                                    typeof existingMsg.content === "string"
                                      ? existingMsg.content.length
                                      : 0,
                                }
                              ];
                              newMessages[index] = {
                                ...existingMsg,
                                tool_calls: newToolCalls,
                              } as any;
                            }
                          } else {
                            newMessages.push({
                              id: messageIdToUse,
                              type: "ai",
                              content: "",
                              tool_calls: [{
                                id: toolUseId,
                                name: toolName,
                                args: "",
                                status: "pending" as const,
                                // The message is being created for this call, so
                                // no answer text can precede it.
                                contentOffset: 0,
                              }],
                            } as any);
                          }
                          
                          return {
                            ...prev,
                            messages: newMessages,
                          };
                        });
                      }
                    }
                  } else if (eventObj.contentBlockDelta) {
                    eventData = eventObj.contentBlockDelta;
                    const delta = eventData.delta || {};
                    const text = delta.text || delta.reasoningContent?.text || "";
                    const isReasoning = !!delta.reasoningContent;
                    const toolUseDelta = delta.toolUse;
                    
                    // Handle tool use input streaming for Strands Agent SDK
                    if (toolUseDelta && toolUseDelta.input) {
                      const toolInput = toolUseDelta.input;
                      const messageIdToUse: string = currentMessageId || `msg-${Date.now()}`;
                      if (!currentMessageId) {
                        currentMessageId = messageIdToUse;
                      }
                      
                      // Find tool call by matching the toolUseId from the delta
                      // If currentToolUseId is not set, find the last pending tool call
                      //
                      // Snapshot the id before queuing the updater: React runs functional
                      // updaters lazily, so reading the mutable `currentToolUseId` inside
                      // one sees whatever it is by then — null once the block has closed.
                      // The "last pending" fallback below hid that here, but it routes to
                      // the wrong call when two calls stream in one message.
                      const routeToolUseId = currentToolUseId;
                      setState((prev) => {
                        const existingMessages = prev.messages || [];
                        const newMessages = [...existingMessages];
                        
                        const index = newMessages.findIndex(
                          (m) => m.id === messageIdToUse && m.type === "ai"
                        );
                        
                        if (index >= 0) {
                          const existingMsg = newMessages[index];
                          const existingToolCalls = (existingMsg as any).tool_calls || [];
                          
                          // Try to find tool call by the snapshotted id first
                          let toolCallIndex = -1;
                          if (routeToolUseId) {
                            toolCallIndex = existingToolCalls.findIndex(
                              (tc: any) => tc.id === routeToolUseId
                            );
                          }
                          
                          // If not found, find the last pending tool call
                          if (toolCallIndex === -1) {
                            for (let i = existingToolCalls.length - 1; i >= 0; i--) {
                              if (existingToolCalls[i].status === "pending") {
                                toolCallIndex = i;
                                break;
                              }
                            }
                          }
                          
                          if (toolCallIndex >= 0) {
                            const toolCall = existingToolCalls[toolCallIndex];
                            // Accumulate tool input as string, will be parsed later
                            const currentInput = typeof toolCall.args === "string" 
                              ? toolCall.args 
                              : (typeof toolCall.args === "object" && toolCall.args !== null
                                  ? JSON.stringify(toolCall.args)
                                  : "");
                            
                            // Create new tool calls array with immutable update
                            const newToolCalls = existingToolCalls.map((tc: any, idx: number) =>
                              idx === toolCallIndex
                                ? { ...tc, args: currentInput + toolInput }
                                : tc
                            );
                            
                            newMessages[index] = {
                              ...existingMsg,
                              tool_calls: newToolCalls,
                            } as any;
                          }
                        }
                        
                        return {
                          ...prev,
                          messages: newMessages,
                        };
                      });
                    } else if (text) {
                      // Use existing messageId or generate one if messageStart didn't come first
                      const messageIdToUse: string = currentMessageId || `msg-${Date.now()}`;
                      if (!currentMessageId) {
                        currentMessageId = messageIdToUse;
                      }
                      
                      // Backend already handles duplicate checking and delta extraction
                      // Frontend just needs to append the delta received
                      if (isReasoning) {
                        // Handle reasoning delta - simply append
                        setState((prev) => {
                          const existingMessages = prev.messages || [];
                          const newMessages = [...existingMessages];
                          
                          const index = newMessages.findIndex(
                            (m) => m.id === messageIdToUse && m.type === "ai"
                          );
                          
                          if (index >= 0) {
                            const existingMsg = newMessages[index];
                            const existingReasoning = (existingMsg as any).reasoning || "";
                            newMessages[index] = {
                              ...existingMsg,
                              reasoning: existingReasoning + text,
                            } as any;
                          } else {
                            newMessages.push({
                              id: messageIdToUse,
                              type: "ai",
                              content: "",
                              reasoning: text,
                            } as any);
                          }

                          return {
                            ...prev,
                            messages: newMessages,
                            // Real content supersedes the placeholder: the
                            // status line occupies the spot the answer fills.
                            status: undefined,
                          };
                        });
                      } else {
                        // Handle content delta - simply append
                        setState((prev) => {
                          const existingMessages = prev.messages || [];
                          const newMessages = [...existingMessages];
                          
                          const index = newMessages.findIndex(
                            (m) => m.id === messageIdToUse && m.type === "ai"
                          );
                          
                          if (index >= 0) {
                            const existingMsg = newMessages[index];
                            const existingContent = typeof existingMsg.content === "string" 
                              ? existingMsg.content 
                              : "";
                            newMessages[index] = {
                              ...existingMsg,
                              content: existingContent + text,
                            };
                          } else {
                            newMessages.push({
                              id: messageIdToUse,
                              type: "ai",
                              content: text,
                            });
                          }

                          return {
                            ...prev,
                            messages: newMessages,
                            // Real content supersedes the placeholder: the
                            // status line occupies the spot the answer fills.
                            status: undefined,
                          };
                        });
                      }
                    }
                  } else if (eventObj.contentBlockStop) {
                    // A tool-use block closed: its streamed argument text is complete,
                    // so parse it and settle the call. The rule lives in closeToolUse
                    // (see closeToolUse.test.mjs).
                    eventData = eventObj.contentBlockStop;
                    const messageIdToUse: string = currentMessageId || `msg-${Date.now()}`;

                    // Snapshot the id *before* queuing the updater. React runs functional
                    // updaters lazily, and `currentToolUseId` is reset right below — reading
                    // the mutable variable inside the updater matched nothing whenever the
                    // stop arrived in the same chunk as a burst of deltas, and the arguments
                    // stayed a string forever (the card app then received `{}`).
                    const closingToolUseId = currentToolUseId;
                    if (closingToolUseId) {
                      setState((prev) => ({
                        ...prev,
                        messages: closeToolUse(prev.messages, messageIdToUse, closingToolUseId),
                      }));
                    }

                    currentToolUseId = null;
                  } else if (eventObj.messageStop) {
                    eventData = eventObj.messageStop;
                    const messageIdToUse = currentMessageId || `msg-${Date.now()}`;
                    currentMessageId = null;
                    currentToolUseId = null;
                    
                    setState((prev) => {
                      const existingMessages = prev.messages || [];
                      const newMessages = [...existingMessages];
                      
                      const index = newMessages.findIndex(
                        (m) => m.id === messageIdToUse && m.type === "ai"
                      );

                      // Message stop doesn't provide full text, so we keep accumulated content
                      if (index >= 0) {
                        // Runtimes that execute tools server-side never send a
                        // toolResult event, so settle any still-pending call here
                        // instead of leaving its spinner running forever.
                        const toolCalls = (newMessages[index] as any).tool_calls;
                        newMessages[index] = {
                          ...newMessages[index],
                          ...(toolCalls
                            ? {
                                tool_calls: toolCalls.map((tc: any) =>
                                  tc.status === "pending"
                                    ? { ...tc, status: "completed" as const }
                                    : tc
                                ),
                              }
                            : {}),
                        } as any;
                      }

                      return {
                        ...prev,
                        messages: newMessages,
                        // A message ended but the turn has not (no `end` yet):
                        // a tool loop, the model composes its next step now and
                        // that gap streams nothing — measured at ~8s for a
                        // bap_default artifact turn. Without a marker the screen
                        // reads as frozen, so re-arm the progress line (timer
                        // continues via the preserved startedAt). The next
                        // content token clears it; the terminal `end` clears it
                        // too, so the final stop only flickers it imperceptibly.
                        status: {
                          label: "응답 생성 중",
                          startedAt: prev.status?.startedAt ?? Date.now(),
                        },
                      };
                    });
                  } else if (eventObj.agentStatus) {
                    // Transient progress, not content: it fills the silence
                    // before the first token and is replaced by the answer.
                    // Deliberately not written into `messages` — it must not
                    // survive into the saved transcript.
                    eventData = eventObj.agentStatus;
                    const label = eventData.label;
                    if (label) {
                      setState((prev) => ({
                        ...prev,
                        status: {
                          label,
                          phase: eventData.phase,
                          // Held across updates so the timer counts the whole
                          // wait, not the time since the latest heartbeat.
                          startedAt: prev.status?.startedAt ?? Date.now(),
                        },
                      }));
                    }
                  } else if (eventObj.metadata) {
                    eventData = eventObj.metadata;
                    // Metadata can be stored if needed
                  } else if (eventObj.artifact) {
                    // Agent produced a document the side panel renders. The
                    // server has already stored it and resolved its version.
                    eventData = eventObj.artifact;
                    const artifact = eventData;

                    setState((prev) => {
                      const existing = ((prev.values as any)?.artifacts ??
                        []) as any[];
                      const index = existing.findIndex(
                        (a) =>
                          a.artifactId === artifact.artifactId &&
                          a.version === artifact.version
                      );
                      const artifacts =
                        index >= 0
                          ? existing.map((a, i) =>
                              i === index ? { ...a, ...artifact } : a
                            )
                          : [...existing, artifact];

                      return {
                        ...prev,
                        values: {
                          ...prev.values,
                          artifacts,
                        } as T,
                      };
                    });
                  } else if (eventObj.mcpApp) {
                    // Handle MCP App UI resource from agent
                    eventData = eventObj.mcpApp;

                    // Store MCP App metadata in state for rendering McpAppView.
                    //
                    // recordId comes from the event, not from the agent being
                    // chatted with: the relay resolves endpoints out of MCP
                    // records, and an agent's own (A2A) record has none — using
                    // it made every app fail with 400. The server stamps the MCP
                    // record that actually serves this ui:// resource.
                    setState((prev) => {
                      const existingMcpApps = (prev.values as any)?.mcpApps || [];
                      const entry = {
                        recordId: eventData.recordId,
                        toolCallId: eventData.toolCallId,
                        toolName: eventData.toolName,
                        resourceUri: eventData.resourceUri,
                        messageId: eventData.messageId,
                      };

                      return {
                        ...prev,
                        values: {
                          ...prev.values,
                          mcpApps: [...existingMcpApps, entry],
                        } as T,
                      };
                    });
                  } else if (eventObj.toolResult) {
                    // Attach the result to its call, and mark the call failed
                    // if the tool said it failed. Both the placement rule and
                    // the status mapping live in applyToolResult, which is
                    // where the overwrite contract with the server is
                    // documented — a chunked result arrives as many events,
                    // each carrying the running total.
                    eventData = eventObj.toolResult;

                    setState((prev) => ({
                      ...prev,
                      messages: applyToolResult(prev.messages, eventData),
                    }));
                  } else if (eventObj.chart || eventObj.verification) {
                    // Both arrive after the answer text and belong to the turn
                    // that produced them, so they attach to the assistant
                    // message rather than standing alone. Appended to the last
                    // ai message: the runtime keeps one messageId across its
                    // whole tool loop, so that is the message being written.
                    const isChart = Boolean(eventObj.chart);
                    eventData = isChart ? eventObj.chart : eventObj.verification;
                    const payload = eventData;
                    setState((prev) => {
                      const newMessages = [...(prev.messages || [])];
                      let index = -1;
                      for (let i = newMessages.length - 1; i >= 0; i--) {
                        if (newMessages[i].type === "ai") {
                          index = i;
                          break;
                        }
                      }
                      // A chart with no message to attach to would be lost, so
                      // one is opened for it — the turn did produce something.
                      if (index < 0) {
                        newMessages.push({
                          id: `msg-${Date.now()}`,
                          type: "ai",
                          content: "",
                        } as any);
                        index = newMessages.length - 1;
                      }
                      const msg = newMessages[index] as any;
                      const key = isChart ? "charts" : "verifications";
                      newMessages[index] = {
                        ...msg,
                        [key]: [...(msg[key] || []), payload],
                      };
                      return {
                        ...prev,
                        messages: newMessages,
                        // Real output supersedes the progress placeholder.
                        status: undefined,
                      };
                    });
                  } else if (eventObj.error) {
                    eventData = eventObj.error;
                    // A failure inside the runtime arrives in-band on a 200
                    // stream, so the fetch-level catch below never sees it.
                    // Route it into the same `error` state the refused-request
                    // path uses, or the banner never shows and the run just
                    // ends silently.
                    const errorMessage = eventData.error || "Unknown error";
                    setState((prev) => ({
                      ...prev,
                      isLoading: false,
                      status: undefined,
                      error: errorMessage,
                    }));
                    onError?.(new Error(errorMessage));
                  } else if (eventObj.end) {
                    eventData = eventObj.end;
                    setState((prev) => ({
                      ...prev,
                      isLoading: false,
                      status: undefined,
                    }));
                    onFinish?.();
                  }
                } else {
                  // Legacy format: {"event": "message_start", "data": {...}}
                  const event = data.event || "data";
                  eventData = data.data || {};

                  if (event === "run_baseline") {
                    // First frame of a re-attach: which stored messages predate
                    // the run (plus its own question). The rest of the stored
                    // copy is this run's partial work, which the replay that
                    // follows rebuilds under the same ids — kept, it would
                    // double up. See resumeRun.mjs.
                    const ids = Array.isArray(eventData.message_ids) ? eventData.message_ids : undefined;
                    setState((prev) => {
                      const messages = applyRunBaseline(prev.messages, ids) as Message[];
                      return {
                        ...prev,
                        messages,
                        values: { ...prev.values, messages } as T,
                      };
                    });
                  } else if (event === "thread_created" || event === "created") {
                    const newThreadId = eventData.thread_id || targetThreadId;
                    if (currentThreadIdRef.current === targetThreadId || currentThreadIdRef.current === null) {
                      currentThreadIdRef.current = newThreadId;
                      onThreadId?.(newThreadId);
                      onCreated?.();
                    }
                  } else if (event === "message_start") {
                    currentMessageId = eventData.message_id || null;
                  } else if (event === "content_delta") {
                    // Handle streaming content delta from Bedrock
                    // Server always provides message_id, use it directly
                    const deltaText = eventData.text || "";
                    const messageId = eventData.message_id;
                    
                    if (!messageId) {
                      console.warn("content_delta event missing message_id");
                      continue; // Skip if no message_id
                    }
                    
                    // Update stored message_id if we got one
                    if (!currentMessageId) {
                      currentMessageId = messageId;
                    }
                    
                    if (deltaText) {
                      setState((prev) => {
                        const existingMessages = prev.messages || [];
                        const newMessages = [...existingMessages];
                        
                        // Find existing AI message with same ID (streaming update)
                        const index = newMessages.findIndex(
                          (m) => m.id === messageId && m.type === "ai"
                        );
                        
                        if (index >= 0) {
                          // Accumulate delta to existing message
                          const existingMsg = newMessages[index];
                          const existingContent = typeof existingMsg.content === "string" 
                            ? existingMsg.content 
                            : "";
                          newMessages[index] = {
                            ...existingMsg,
                            content: existingContent + deltaText,
                          };
                        } else {
                          // Create new AI message with delta (first delta for this message)
                          newMessages.push({
                            id: messageId,
                            type: "ai",
                            content: deltaText,
                          });
                        }
                        
                        return {
                          ...prev,
                          messages: newMessages,
                        };
                      });
                    }
                  } else if (event === "reasoning_delta") {
                    // Handle streaming reasoning content from Bedrock
                    const deltaReasoning = eventData.text || "";
                    const messageId = eventData.message_id;
                    
                    if (!messageId) {
                      console.warn("reasoning_delta event missing message_id");
                      continue;
                    }
                    
                    if (deltaReasoning) {
                      setState((prev) => {
                        const existingMessages = prev.messages || [];
                        const newMessages = [...existingMessages];
                        
                        // Find existing AI message with same ID
                        const index = newMessages.findIndex(
                          (m) => m.id === messageId && m.type === "ai"
                        );
                        
                        if (index >= 0) {
                          // Accumulate reasoning delta
                          const existingMsg = newMessages[index];
                          const existingReasoning = existingMsg.reasoning || "";
                          newMessages[index] = {
                            ...existingMsg,
                            reasoning: existingReasoning + deltaReasoning,
                          };
                        } else {
                          // Create new AI message with reasoning
                          newMessages.push({
                            id: messageId,
                            type: "ai",
                            content: "",
                            reasoning: deltaReasoning,
                          });
                        }
                        
                        return {
                          ...prev,
                          messages: newMessages,
                        };
                      });
                    }
                  } else if (event === "message_stop") {
                    // Handle final message from Bedrock
                    // Server always provides message_id
                    const fullText = eventData.full_text || "";
                    const fullReasoning = eventData.full_reasoning || null;
                    const messageId = eventData.message_id;
                    
                    if (!messageId) {
                      console.warn("message_stop event missing message_id");
                      continue; // Skip if no message_id
                    }
                    
                    currentMessageId = null; // Reset after message_stop
                    
                    setState((prev) => {
                      const existingMessages = prev.messages || [];
                      const newMessages = [...existingMessages];
                      
                      // Find existing AI message and overwrite with full text
                      const index = newMessages.findIndex(
                        (m) => m.id === messageId && m.type === "ai"
                      );
                      
                      if (index >= 0) {
                        // Overwrite with full text and reasoning
                        newMessages[index] = {
                          ...newMessages[index],
                          content: fullText,
                          ...(fullReasoning !== null && { reasoning: fullReasoning }),
                        };
                      } else {
                        // Add new AI message with full text
                        newMessages.push({
                          id: messageId,
                          type: "ai",
                          content: fullText,
                        });
                      }
                      
                      return {
                        ...prev,
                        messages: newMessages,
                      };
                    });
                  } else if (event === "messages") {
                    // Handle messages event (for backward compatibility)
                    const messages = eventData.messages || [];
                    setState((prev) => {
                      const existingMessages = prev.messages || [];
                      const newMessages = [...existingMessages];
                      
                      messages.forEach((msg: Message) => {
                        if (msg.type === "ai") {
                          const index = newMessages.findIndex(
                            (m) => m.id === msg.id && m.type === "ai"
                          );
                          if (index >= 0) {
                            newMessages[index] = msg;
                          } else {
                            newMessages.push(msg);
                          }
                        } else if (msg.type === "human") {
                          const index = newMessages.findIndex(
                            (m) => m.id === msg.id && m.type === "human"
                          );
                          if (index < 0) {
                            newMessages.push(msg);
                          }
                        } else {
                          newMessages.push(msg);
                        }
                      });
                      
                      return {
                        ...prev,
                        messages: newMessages,
                      };
                    });
                  } else if (event === "data") {
                    // Update state values
                    setState((prev) => ({
                      ...prev,
                      values: { ...prev.values, ...eventData },
                    }));
                  } else if (event === "end") {
                    setState((prev) => ({
                      ...prev,
                      isLoading: false,
                      status: undefined,
                    }));
                    onFinish?.();
                  } else if (event === "error") {
                    // Same reasoning as the Strands-format error branch above:
                    // in-band failures must land in `error` state to be seen.
                    const errorMessage = eventData.error || "Stream error";
                    setState((prev) => ({
                      ...prev,
                      isLoading: false,
                      status: undefined,
                      error: errorMessage,
                    }));
                    onError?.(new Error(errorMessage));
                  }
                }
              } catch (e) {
                // Log JSON parse errors with the problematic line for debugging
                console.warn("Failed to parse SSE event:", e);
                console.warn("Problematic line:", line);
                console.warn("Line length:", line.length);
              }
            }
          }

          if (streamEnded) break;
        }
      } finally {
        reader.releaseLock();
        // However this stream ended, nothing more is coming through it. A
        // tool call still marked pending here has no other way out: its
        // result and the `messageStop` that would have settled it both
        // travelled on this connection. Without this, a turn cut short mid
        // tool call leaves a spinner running until the page is reloaded.
        //
        // Safe on a clean end, where every call was already settled — the
        // helper returns the same array untouched.
        //
        // `isLoading` is cleared here for the same reason: the terminal `end`
        // event is the only other thing that clears it, and a stream can end
        // without one — the proxy severs an idle upstream at 30s, or the
        // origin closes after the answer but before `end` reaches the client.
        // Then `reader.read()` resolves `done` rather than throwing, so the
        // `.catch` below never runs, and the global spinner turned forever
        // even though the whole answer had already arrived.
        //
        // Nothing at all on an aborted stream, like the `.catch`: the state no
        // longer belongs to this loop. A thread switch aborts it and the new
        // thread's view owns `isLoading` — and, now that a run outlives its
        // subscribers, that view may be a re-attach to this very run, whose
        // tool calls are still legitimately pending. Settling them here marked
        // a running tool as finished on the screen that had just reopened it.
        // Stop settles its own view in `stop()`.
        if (!abortController.signal.aborted) {
          setState((prev) => ({
            ...prev,
            messages: settlePendingToolCalls(prev.messages),
            isLoading: false,
            status: undefined,
          }));
        }
      }
    },
    [onThreadId, onFinish, onError, onCreated]
  );
  // Read through a ref by `submit` and the sync effect. `consume` is rebuilt
  // whenever a caller passes a fresh inline callback (useChat's onFinish is one),
  // and an effect depending on it would re-run on every render — clearing
  // adoptedThreadIdRef before the first turn's thread_created round-trip, so a
  // run's own new thread read as a switch away from it.
  const consumeRef = useRef(consume);
  consumeRef.current = consume;

  const submit = useCallback(
    (
      values?: Record<string, any> | null,
      options?: {
        optimisticValues?: (prev: T) => T;
        config?: Record<string, any>;
        checkpoint?: any;
        interruptBefore?: string[];
        interruptAfter?: string[];
        command?: any;
      }
    ) => {
      // Stop any existing stream
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      // Preserve existing messages when starting a new stream
      setState((prev) => {
        // Ensure we keep existing messages when starting stream
        const currentMessages = prev.messages.length > 0 
          ? prev.messages 
          : ((prev.values as any)?.messages as Message[]) || [];
        
        return {
          ...prev,
          messages: currentMessages, // Preserve existing messages
          isLoading: true,
          // Show a spinner + timer from the instant the user submits, not only
          // once the runtime's first agentStatus arrives — a turn can be several
          // seconds silent before the model emits anything, and a bare gap there reads as a frozen screen. The
          // agentStatus handler below keeps this startedAt (`?? Date.now()`), so
          // the timer counts the whole wait and the label swaps in seamlessly;
          // the first answer token clears it.
          status: { label: "시작하는 중", startedAt: Date.now() },
          // Clear the previous failure: leaving it up would make a retry look
          // like it had failed again the moment it started.
          error: undefined,
        };
      });

      // Prepare request
      const requestBody: any = {};
      if (values) {
        requestBody.values = values;
      }
      if (options?.config) {
        requestBody.config = options.config;
      }
      if (options?.checkpoint) {
        requestBody.checkpoint = options.checkpoint;
      }
      if (options?.command) {
        requestBody.command = options.command;
      }
      if (options?.interruptBefore) {
        requestBody.interrupt_before = options.interruptBefore;
      }
      if (options?.interruptAfter) {
        requestBody.interrupt_after = options.interruptAfter;
      }

      // Apply optimistic values
      if (options?.optimisticValues) {
        setState((prev) => {
          const currentMessages = prev.messages.length > 0 
            ? prev.messages 
            : ((prev.values as any)?.messages as Message[]) || [];
          
          const newValues = options.optimisticValues!({
            ...prev.values,
            messages: currentMessages,
          } as T);
          
          // Extract messages from optimistic values if present
          const optimisticMessages = (newValues as any)?.messages;
          const newMessages = optimisticMessages || currentMessages;
          
          return {
            ...prev,
            values: newValues,
            messages: newMessages,
          };
        });
      }

      // Use current thread ID or create new one
      // If threadId prop is null, always generate a new UUID (don't reuse ref)
      // This ensures we create a new thread when starting fresh
      const targetThreadId = threadId ?? (currentThreadIdRef.current || randomId());

      // Claim the id before the request goes out, so that when it comes back as a
      // changed `threadId` prop the sync effect recognises it as this run's own
      // doing rather than a thread switch to abort. See adoptedThreadIdRef.
      if (threadId !== targetThreadId) {
        adoptedThreadIdRef.current = targetThreadId;
        currentThreadIdRef.current = targetThreadId;
      }

      // Start streaming. One retry on "a run is still in flight": right after
      // Stop the server is still saving the interrupted turn for a moment, and
      // a user who stops and immediately sends again should not see an error
      // for that. A second 409 is reported — something is genuinely running.
      const startRun = async (): Promise<ReadableStream<Uint8Array>> => {
        try {
          return await apiClient.streamThread(targetThreadId, requestBody, abortController.signal);
        } catch (error) {
          if (!(error instanceof RunInFlightError) || abortController.signal.aborted) throw error;
          await new Promise((resolve) => setTimeout(resolve, 800));
          if (abortController.signal.aborted) throw error;
          return await apiClient.streamThread(targetThreadId, requestBody, abortController.signal);
        }
      };
      startRun()
        .then((stream) => consumeRef.current(stream, abortController, targetThreadId))
        .catch((error) => {
          if (!abortController.signal.aborted) {
            setState((prev) => ({
              ...prev,
              isLoading: false,
              status: undefined,
              // Same reasoning as the `finally` above: a request that dies before
              // or during the stream never delivers the frames that settle its
              // tool calls, and this is the last place that can.
              messages: settlePendingToolCalls(prev.messages),
              // Surfaced in the UI, not just handed to onError: the callers use
              // that purely to revalidate the thread list, so a refused request
              // would otherwise leave no trace on screen at all.
              error: error instanceof Error ? error.message : String(error),
            }));
            onError?.(error);
          }
        });
    },
    [threadId, apiClient, onError]
  );

  const stop = useCallback(() => {
    // The run lives on the server now; the abort below only closes this tab's
    // view of it. Tell the server, fire-and-forget: the frames that settle the
    // UI are produced locally right after.
    const runningThreadId = currentThreadIdRef.current;
    if (runningThreadId) {
      void apiClient.cancelRun(runningThreadId).catch(() => {});
    }
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setState((prev) => {
      // Tool calls the user cut off. An MCP App mounted on one of these must be
      // told `ui/notifications/tool-cancelled` rather than left waiting for a
      // result (or, worse, re-running the tool to fetch one). Settling below
      // turns them into plain "completed" calls, so the ids are recorded first.
      const cancelled: string[] = [];
      for (const m of prev.messages || []) {
        for (const tc of ((m as any).tool_calls || []) as any[]) {
          if (tc?.status === "pending" && tc.id) cancelled.push(tc.id);
        }
      }
      const prior = ((prev.values as any)?.cancelledToolCalls as string[] | undefined) ?? [];
      return {
        ...prev,
        isLoading: false,
        status: undefined,
        values: cancelled.length
          ? ({ ...prev.values, cancelledToolCalls: [...prior, ...cancelled] } as T)
          : prev.values,
        // The abort silences the stream, so a tool call in flight would keep its
        // spinner: the `catch` below deliberately skips an aborted request, and the
        // server's own frames are no longer being read. Stopping is the user saying
        // the turn is over, which is exactly when nothing may still look busy.
        messages: settlePendingToolCalls(prev.messages),
      };
    });
  }, [apiClient]);

  // Update thread ID when it changes and reset state if switching threads
  useEffect(() => {
    const loadedThreadId = loadedThreadIdRef.current;
    // The id this run gave itself, arriving back as a prop. Nothing switched, so
    // the stream in flight is the one that owns this thread — leave it alone and
    // let it finish; its own events are already building the state.
    const isOwnNewThread =
      threadId !== null && adoptedThreadIdRef.current === threadId;

    currentThreadIdRef.current = threadId;
    loadedThreadIdRef.current = threadId;
    if (isOwnNewThread) {
      // One-shot: a later, genuine switch back to this thread must still load it.
      adoptedThreadIdRef.current = null;
      return;
    }
    // Any other change means we are leaving whatever this run was doing.
    adoptedThreadIdRef.current = null;

    // If threadId changed (including to/from null, and including the first
    // render, where nothing has been loaded yet), cancel any ongoing stream and
    // reset state
    if (loadedThreadId !== threadId) {
      // Drop this tab's view of any stream in flight. The run itself carries
      // on server-side and is re-attached below if we come back to it busy.
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      
      // Reset loading state
      setState((prev) => ({
        ...prev,
        isLoading: false,
        status: undefined,
      }));
      
      // If switching to an existing thread, load its data
      if (threadId) {
        // Set loading state immediately
        setState({
          values: {} as T,
          messages: [],
          isLoading: false,
          status: undefined,
          isThreadLoading: true,
        });
        
        // Load thread data when switching to an existing thread
        // getThread already returns all thread data including values.messages
        apiClient
          .getThread(threadId)
          .then((thread: any) => {
            // Only update state if we're still on the same thread (prevent race conditions)
            if (currentThreadIdRef.current === threadId) {
              // Thread structure: { thread_id, values, status, metadata, ... }
              // Messages are stored in thread.values.messages
              const threadValues = thread.values || {};
              const threadMessages = Array.isArray(threadValues.messages) ? threadValues.messages : [];

              if (thread.status === "busy") {
                // A run is in flight on the server. Show the stored transcript and
                // attach: the attach stream opens with a `run_baseline` frame that
                // trims it to what predates the run, then replays the run itself.
                setState({
                  values: { ...threadValues, messages: threadMessages } as T,
                  messages: threadMessages,
                  isLoading: true,
                  status: { label: "이어서 받는 중", startedAt: Date.now() },
                  isThreadLoading: false,
                });
                const abortController = new AbortController();
                abortControllerRef.current = abortController;
                apiClient
                  .attachThread(threadId, abortController.signal)
                  .then((stream) => consumeRef.current(stream, abortController, threadId))
                  .catch(async (error) => {
                    if (abortController.signal.aborted || currentThreadIdRef.current !== threadId) return;
                    if (error instanceof NoActiveRunError) {
                      // Finished between the read and the attach: the record is
                      // complete now, show it.
                      try {
                        const done: any = await apiClient.getThread(threadId);
                        if (currentThreadIdRef.current !== threadId) return;
                        const values = done.values || {};
                        const messages = Array.isArray(values.messages) ? values.messages : [];
                        setState({
                          values: { ...values, messages } as T,
                          messages,
                          isLoading: false,
                          status: undefined,
                          isThreadLoading: false,
                        });
                      } catch (reloadError) {
                        console.warn("Failed to reload finished thread:", reloadError);
                        setState((prev) => ({ ...prev, isLoading: false, status: undefined }));
                      }
                      return;
                    }
                    setState((prev) => ({
                      ...prev,
                      isLoading: false,
                      status: undefined,
                      error: error instanceof Error ? error.message : String(error),
                    }));
                  });
                return;
              }

              // Set state with all messages from thread
              setState({
                values: { ...threadValues, messages: threadMessages } as T,
                messages: threadMessages,
                isLoading: false,
                status: undefined,
                isThreadLoading: false,
              });
            }
          })
          .catch((error) => {
            console.warn("Failed to load thread:", error);
            // Only update state if we're still on the same thread
            if (currentThreadIdRef.current === threadId) {
              setState((prev) => ({ ...prev, isThreadLoading: false }));
            }
          });
      } else {
        // threadId is null - clear everything for new thread
        setState({
          values: {} as T,
          messages: [],
          isLoading: false,
          status: undefined,
          isThreadLoading: false,
        });
      }
    }
  }, [threadId, apiClient]);

  return {
    ...state,
    submit,
    stop,
    addMessage,
  };
}

