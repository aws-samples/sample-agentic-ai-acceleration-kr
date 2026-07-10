// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useCallback, useRef, useState } from 'react';
import type { StreamEvent } from './types';

// admin-ui server-side proxy 경유 (api.ts 와 동일). SSE 스트림도 pass-through.
const API_BASE = '/api/chat-proxy';

export interface ChatStreamOptions {
  onEvent: (event: StreamEvent) => void;
  onError?: (error: Error) => void;
}

/**
 * SSE 수신 hook. AgentCore 가 실제로 어떤 이벤트 형태를 보내는지에 따라 파싱
 * 룰이 달라질 수 있어 단순/관대하게 구현. event:/data: 표준 SSE 만 처리.
 */
export function useChatStream({ onEvent, onError }: ChatStreamOptions) {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(
    async (
      sessionId: string,
      content: string,
      screenContext?: unknown,
      mode: 'quick' | 'deep' = 'quick'
    ) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);

      try {
        // screen_context: "지금 보는 화면" 요약(있을 때만). agent 가 컨텍스트
        // 질의에 활용. 없으면 기존과 동일한 {content} 전송.
        const payload: Record<string, unknown> = { content, mode };
        if (screenContext) payload.screen_context = screenContext;
        const response = await fetch(
          `${API_BASE}/admin/chat/sessions/${sessionId}/messages`,
          {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              Accept: 'text/event-stream',
            },
            body: JSON.stringify(payload),
            signal: controller.signal,
          }
        );

        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE 메시지 = '\n\n' 구분
          let idx;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            parseSseBlock(block, onEvent);
          }
        }
        // tail flush
        if (buffer.trim()) parseSseBlock(buffer, onEvent);
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          onError?.(e as Error);
        }
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [onEvent, onError]
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // 진행 중 분석 재구독(§56 핸드오프) — 다른 메뉴에 다녀온 뒤 서버가 background
  // 로 계속 돌리던 스트림을 GET /stream 으로 이어받는다(이미 발행분 재생+실시간).
  // 404(활성 스트림 없음 — 완료/만료)는 정상: history 가 이미 결과를 보여줌.
  const reattach = useCallback(
    async (sessionId: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const response = await fetch(
          `${API_BASE}/admin/chat/sessions/${sessionId}/stream`,
          {
            method: 'GET',
            credentials: 'include',
            headers: { Accept: 'text/event-stream' },
            signal: controller.signal,
          }
        );
        if (response.status === 404) return; // 진행 중 분석 없음 — 무시
        if (!response.ok || !response.body) return;
        setIsStreaming(true);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            parseSseBlock(block, onEvent);
          }
        }
      } catch {
        // 재구독 실패는 무해(완료분은 history 로 복원됨)
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [onEvent]
  );

  return { send, cancel, reattach, isStreaming };
}

function parseSseBlock(block: string, onEvent: (e: StreamEvent) => void) {
  let event = 'message';
  let data = '';
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  if (!data) return;
  try {
    const parsed = JSON.parse(data);
    onEvent({ type: event, ...parsed } as StreamEvent);
  } catch {
    onEvent({ type: 'text', chunk: data } as StreamEvent);
  }
}
