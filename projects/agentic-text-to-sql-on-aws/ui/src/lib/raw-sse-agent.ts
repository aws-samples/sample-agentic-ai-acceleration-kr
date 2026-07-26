// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { AbstractAgent, type HttpAgentFetchFn } from '@ag-ui/client';
import { EventType, type BaseEvent, type RunAgentInput } from '@ag-ui/core';
import { Observable } from 'rxjs';

/**
 * ============================================================================
 * RawSSEAgent — 폴백 어댑터 (ARCHITECTURE.md §8 리스크 완화)
 * ============================================================================
 * 기본 경로(agui)는 @ag-ui/client `HttpAgent` 가 SSE 파싱을 담당한다. 그 통합이
 * 문제를 일으킬 경우(예: 백엔드가 표준 스키마를 내보내지만 HttpAgent 버전 호환 이슈)
 * 이 어댑터로 전환한다. `AGUI_ADAPTER=raw-sse`.
 *
 * ✅ orchestrator 이벤트 계약 확정(2026-07): ag_ui_strands 가 **표준 AG-UI 이벤트**를
 *    `data: {JSON}\n\n` SSE 로 생성한다. 따라서 이 파서는 표준 스키마를 그대로 통과시킨다:
 *      data: {"type":"RUN_STARTED","threadId":"...","runId":"..."}
 *      data: {"type":"TEXT_MESSAGE_START","messageId":"m1","role":"assistant"}
 *      data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"..."}
 *      data: {"type":"TEXT_MESSAGE_END","messageId":"m1"}
 *      data: {"type":"TOOL_CALL_START","toolCallId":"t1","toolCallName":"...","parentMessageId":"m1"}
 *      data: {"type":"TOOL_CALL_ARGS","toolCallId":"t1","delta":"{...}"}
 *      data: {"type":"TOOL_CALL_END","toolCallId":"t1"}
 *      data: {"type":"TOOL_CALL_RESULT","toolCallId":"t1","messageId":"r1","content":"..."}
 *      data: {"type":"RUN_FINISHED","runId":"..."}
 *      data: {"type":"RUN_ERROR","runId":"...","message":"..."}
 *      data: {"type":"STATE_SNAPSHOT","snapshot":{...}} / {"type":"STATE_DELTA","delta":[...]}
 *
 * 파싱: `line.startsWith("data:")` → `JSON.parse(payload)` → BaseEvent 로 emit.
 * 비표준 이벤트 스키마로 폴백할 경우 `normalizeEvent()` 한 곳만 수정하면 된다. (교체 지점)
 */

export interface RawSSEAgentConfig {
  url: string;
  headers: Record<string, string>;
  fetch: HttpAgentFetchFn;
}

/** 알려진 AG-UI 이벤트 타입 집합 (통과 검증용). */
const KNOWN_EVENT_TYPES = new Set<string>(Object.values(EventType));

export class RawSSEAgent extends AbstractAgent {
  private readonly url: string;
  private readonly extraHeaders: Record<string, string>;
  private readonly fetchFn: HttpAgentFetchFn;

  constructor(config: RawSSEAgentConfig) {
    super();
    this.url = config.url;
    this.extraHeaders = config.headers;
    this.fetchFn = config.fetch;
  }

  run(input: RunAgentInput): Observable<BaseEvent> {
    return new Observable<BaseEvent>((subscriber) => {
      const abort = new AbortController();
      let sawRunFinished = false;

      (async () => {
        try {
          const res = await this.fetchFn(this.url, {
            method: 'POST',
            headers: {
              'content-type': 'application/json',
              accept: 'text/event-stream',
              ...this.extraHeaders,
            },
            body: JSON.stringify(input),
            signal: abort.signal,
          } as RequestInit);

          if (!res.ok || !res.body) {
            throw new Error(`AgentCore 응답 오류: HTTP ${res.status}`);
          }

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            // SSE 라인 단위로 data: 파싱 (부분 라인은 버퍼에 남긴다).
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed || !trimmed.startsWith('data:')) continue;
              const payload = trimmed.slice('data:'.length).trim();
              if (!payload || payload === '[DONE]') continue;

              const event = normalizeEvent(payload);
              if (!event) continue;
              if (event.type === EventType.RUN_FINISHED) sawRunFinished = true;
              subscriber.next(event);
            }
          }

          // 백엔드가 RUN_FINISHED 를 보내지 않고 끊긴 경우 보정.
          if (!sawRunFinished) {
            subscriber.next({
              type: EventType.RUN_FINISHED,
              threadId: input.threadId,
              runId: input.runId,
            } as BaseEvent);
          }
          subscriber.complete();
        } catch (err) {
          subscriber.next({
            type: EventType.RUN_ERROR,
            message: err instanceof Error ? err.message : String(err),
          } as BaseEvent);
          subscriber.error(err);
        }
      })();

      // 구독 해제 시 진행 중 요청 중단.
      return () => abort.abort();
    });
  }
}

/**
 * SSE `data:` 페이로드(JSON) → 표준 AG-UI BaseEvent 정규화. **교체 지점.**
 * 표준 스키마는 그대로 통과. 비표준 스키마로 폴백할 경우 여기서 매핑을 조정한다.
 */
function normalizeEvent(payload: string): BaseEvent | null {
  let json: Record<string, unknown>;
  try {
    json = JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return null; // 파싱 불가 라인 무시
  }
  const type = json.type;
  if (typeof type !== 'string' || !KNOWN_EVENT_TYPES.has(type)) {
    return null; // 미지 이벤트 무시 (관측만, 렌더 안 함)
  }
  return json as unknown as BaseEvent;
}
