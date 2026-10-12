"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ArrowUp, Loader2, MessagesSquare, Wrench } from "lucide-react";
import { useClient } from "@/providers/ClientProvider";
import { MarkdownContent } from "@/app/components/MarkdownContent";
import { applyAgentConfig } from "@/lib/agent-config";
import { randomId } from "@/lib/utils";
import type { RegistryRecordDetail } from "@/lib/registry";

interface RecordTryoutProps {
  record: RegistryRecordDetail;
  /** Hands off to the full chat page with this agent selected. */
  onChat: (record: RegistryRecordDetail) => void;
}

/**
 * One-shot try-out chat: one prompt, one streamed answer, no history.
 *
 * Deliberately not useStream/useChat — those manage a persistent thread and
 * URL state. A try-out is a probe: it runs on a throwaway thread that is
 * deleted as soon as the answer settles, so it never accumulates in the
 * user's thread list. Each send replaces the previous exchange.
 */
export function RecordTryout({ record, onChat }: RecordTryoutProps) {
  const apiClient = useClient();
  const [input, setInput] = useState("");
  const [question, setQuestion] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Cancel the stream when the panel unmounts mid-answer.
  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || running) return;

    abortRef.current?.abort();
    const abort = new AbortController();
    abortRef.current = abort;

    setQuestion(text);
    setInput("");
    setAnswer("");
    setActiveTool(null);
    setError(null);
    setRunning(true);

    // Throwaway thread: created by the first (only) turn, deleted below.
    const threadId = randomId();
    const config = applyAgentConfig(
      { recursion_limit: 100 },
      {
        agentRuntimeArn: record.agent_runtime_arn ?? undefined,
        harnessArn: record.harness_arn ?? undefined,
        qualifier: record.qualifier ?? undefined,
        registryRecordId: record.record_id,
        registryAgentName: record.name,
      }
    );

    try {
      const stream = await apiClient.streamThread(threadId, {
        values: {
          messages: [{ id: randomId(), type: "human", content: text }],
        },
        config,
      });
      const reader = stream.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      try {
        while (true) {
          if (abort.signal.aborted) break;
          const { done, value } = await reader.read();
          let lines: string[];
          if (done) {
            buffer += decoder.decode();
            lines = buffer.split("\n");
          } else {
            buffer += decoder.decode(value, { stream: true });
            lines = buffer.split("\n");
            buffer = lines.pop() ?? "";
          }

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let data: any;
            try {
              data = JSON.parse(line.slice(6));
            } catch {
              continue;
            }
            const ev = data.event;
            if (!ev || typeof ev !== "object") continue;

            if (ev.contentBlockStart?.start?.toolUse?.name) {
              setActiveTool(ev.contentBlockStart.start.toolUse.name);
            } else if (ev.contentBlockDelta) {
              // Reasoning deltas are skipped: a try-out shows the answer only.
              const t = ev.contentBlockDelta.delta?.text ?? "";
              if (t) {
                setActiveTool(null);
                setAnswer((prev) => prev + t);
              }
            } else if (ev.error) {
              setError(ev.error.error || "실행 중 오류가 발생했습니다.");
            } else if (ev.end) {
              setActiveTool(null);
            }
          }
          if (done) break;
        }
      } finally {
        reader.releaseLock();
      }
    } catch (e) {
      if (!abort.signal.aborted) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setRunning(false);
      setActiveTool(null);
      // Best-effort: an orphaned throwaway thread is cosmetic.
      apiClient.deleteThread(threadId).catch(() => {});
    }
  }, [apiClient, input, running, record]);

  return (
    <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3">
      <p className="caps-label-xs text-muted-foreground">간단히 테스트</p>

      {question && (
        <div className="space-y-2">
          <p className="rounded-md bg-accent/60 px-2.5 py-1.5 text-xs">
            {question}
          </p>
          {activeTool && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Wrench className="size-3 animate-pulse" />
              도구 실행 중: <span className="font-mono">{activeTool}</span>
            </p>
          )}
          {answer && (
            <div className="rounded-md border border-border bg-background px-2.5 py-2">
              <MarkdownContent
                content={answer}
                isStreaming={running}
                className="text-xs"
              />
            </div>
          )}
          {running && !answer && !activeTool && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              응답을 기다리는 중…
            </p>
          )}
          {error && (
            <p className="text-xs text-destructive">{error}</p>
          )}
          {!running && (answer || error) && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onChat(record)}
            >
              <MessagesSquare className="size-3.5" />
              계속 대화하기
            </Button>
          )}
        </div>
      )}

      <div className="flex items-end gap-1.5">
        <Textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="이 에이전트에게 물어보기…"
          rows={1}
          className="min-h-9 flex-1 resize-none text-xs"
          disabled={running}
        />
        <Button
          size="icon-sm"
          onClick={() => void send()}
          disabled={running || !input.trim()}
          aria-label="보내기"
        >
          {running ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <ArrowUp className="size-3.5" />
          )}
        </Button>
      </div>
    </div>
  );
}
