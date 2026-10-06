import type { Interrupt, Message } from "@/lib/api-types";
import { HumanInterrupt } from "@/app/types/inbox";
import type { ToolCall } from "@/app/types/types";
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function extractStringFromMessageContent(message: Message): string {
  return typeof message.content === "string"
    ? message.content
    : Array.isArray(message.content)
    ? message.content
        .filter(
          (c: unknown) =>
            (typeof c === "object" &&
              c !== null &&
              "type" in c &&
              (c as { type: string }).type === "text") ||
            typeof c === "string"
        )
        .map((c: unknown) =>
          typeof c === "string"
            ? c
            : typeof c === "object" && c !== null && "text" in c
            ? (c as { text?: string }).text || ""
            : ""
        )
        .join("")
    : "";
}

export function extractSubAgentContent(data: unknown): string {
  if (typeof data === "string") {
    return data;
  }

  if (data && typeof data === "object") {
    const dataObj = data as Record<string, unknown>;

    // Try to extract description first
    if (dataObj.description && typeof dataObj.description === "string") {
      return dataObj.description;
    }

    // Then try prompt
    if (dataObj.prompt && typeof dataObj.prompt === "string") {
      return dataObj.prompt;
    }

    // For output objects, try result
    if (dataObj.result && typeof dataObj.result === "string") {
      return dataObj.result;
    }

    // Fallback to JSON stringification
    return JSON.stringify(data, null, 2);
  }

  // Fallback for any other type
  return JSON.stringify(data, null, 2);
}

export function isPreparingToCallTaskTool(messages: Message[]): boolean {
  const lastMessage = messages[messages.length - 1];
  return (
    (lastMessage.type === "ai" &&
      lastMessage.tool_calls?.some(
        (call: { name?: string }) => call.name === "task"
      )) ||
    false
  );
}

export function formatMessageForLLM(message: Message): string {
  let role: string;
  if (message.type === "human") {
    role = "Human";
  } else if (message.type === "ai") {
    role = "Assistant";
  } else if (message.type === "tool") {
    role = `Tool Result`;
  } else {
    role = message.type || "Unknown";
  }

  const timestamp = message.id ? ` (${message.id.slice(0, 8)})` : "";

  let contentText = "";

  // Extract content text
  if (typeof message.content === "string") {
    contentText = message.content;
  } else if (Array.isArray(message.content)) {
    const textParts: string[] = [];

    message.content.forEach((part: any) => {
      if (typeof part === "string") {
        textParts.push(part);
      } else if (part && typeof part === "object" && part.type === "text") {
        textParts.push(part.text || "");
      }
      // Ignore other types like tool_use in content - we handle tool calls separately
    });

    contentText = textParts.join("\n\n").trim();
  }

  // For tool messages, include additional tool metadata
  if (message.type === "tool") {
    const toolName = (message as any).name || "unknown_tool";
    const toolCallId = (message as any).tool_call_id || "";
    role = `Tool Result [${toolName}]`;
    if (toolCallId) {
      role += ` (call_id: ${toolCallId.slice(0, 8)})`;
    }
  }

  // Handle tool calls from .tool_calls property (for AI messages)
  const toolCallsText: string[] = [];
  if (
    message.type === "ai" &&
    message.tool_calls &&
    Array.isArray(message.tool_calls) &&
    message.tool_calls.length > 0
  ) {
    message.tool_calls.forEach((call: any) => {
      const toolName = call.name || "unknown_tool";
      const toolArgs = call.args ? JSON.stringify(call.args, null, 2) : "{}";
      toolCallsText.push(`[Tool Call: ${toolName}]\nArguments: ${toolArgs}`);
    });
  }

  // Combine content and tool calls
  const parts: string[] = [];
  if (contentText) {
    parts.push(contentText);
  }
  if (toolCallsText.length > 0) {
    parts.push(...toolCallsText);
  }

  if (parts.length === 0) {
    return `${role}${timestamp}: [Empty message]`;
  }

  if (parts.length === 1) {
    return `${role}${timestamp}: ${parts[0]}`;
  }

  return `${role}${timestamp}:\n${parts.join("\n\n")}`;
}

export function formatConversationForLLM(messages: Message[]): string {
  const formattedMessages = messages.map(formatMessageForLLM);
  return formattedMessages.join("\n\n---\n\n");
}

export function getInterruptTitle(interrupt: Interrupt): string {
  try {
    const interruptValue = (interrupt.value as any)?.[0] as HumanInterrupt;
    return interruptValue?.action_request.action ?? "Unknown interrupt";
  } catch (error) {
    console.error("Error getting interrupt title:", error);
    return "Unknown interrupt";
  }
}

/**
 * One segment of a turn: either answer text or a run of tool calls.
 */
export type TurnPart =
  | { kind: "text"; key: string; text: string }
  | { kind: "tools"; key: string; calls: ToolCall[] };

/**
 * Rebuild a turn in the order it actually happened.
 *
 * A whole turn is a single message — the runtime keeps one messageId across its
 * entire tool loop — so `content` holds the concatenated answer and `tool_calls`
 * the calls, with nothing tying the two together. Rendering one after the other
 * therefore put the final answer *above* the calls that produced it.
 *
 * `contentOffset` is the missing link: how much text had been written when each
 * call opened. Cutting `content` at those offsets recovers the interleaving.
 *
 * Calls with no offset predate the field and cannot be placed, so they keep the
 * old behaviour and go last rather than being guessed at.
 */
export function buildTurnTimeline(
  content: string,
  toolCalls: ToolCall[]
): TurnPart[] {
  const visible = toolCalls.filter((tc) => tc.name !== "task");
  const hasText = content.trim() !== "";

  if (visible.length === 0) {
    return hasText ? [{ kind: "text", key: "text-0", text: content }] : [];
  }

  const placed = visible
    .filter((tc) => typeof tc.contentOffset === "number")
    .sort((a, b) => a.contentOffset! - b.contentOffset!);
  const unplaced = visible.filter((tc) => typeof tc.contentOffset !== "number");

  const parts: TurnPart[] = [];
  let cursor = 0;

  for (let i = 0; i < placed.length; ) {
    const groupOffset = placed[i].contentOffset!;
    // Clamped: a truncated turn can leave an offset past the text it was
    // measured against, and a negative slice would duplicate the tail.
    const offset = Math.min(Math.max(groupOffset, 0), content.length);
    const segment = content.slice(cursor, offset);
    if (segment.trim() !== "") {
      parts.push({ kind: "text", key: `text-${cursor}`, text: segment });
    }
    cursor = Math.max(cursor, offset);

    // Calls sharing an offset ran back to back with no text between them, so
    // they belong in one group and stay visually adjacent.
    const group: ToolCall[] = [];
    while (i < placed.length && placed[i].contentOffset === groupOffset) {
      group.push(placed[i]);
      i += 1;
    }
    parts.push({ kind: "tools", key: `tools-${group[0].id}`, calls: group });
  }

  const tail = content.slice(cursor);
  if (tail.trim() !== "") {
    parts.push({ kind: "text", key: `text-${cursor}`, text: tail });
  }
  if (unplaced.length > 0) {
    parts.push({
      kind: "tools",
      key: `tools-${unplaced[0].id}`,
      calls: unplaced,
    });
  }
  return parts;
}
