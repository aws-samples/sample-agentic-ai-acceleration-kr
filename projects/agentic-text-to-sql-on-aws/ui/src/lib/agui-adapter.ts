// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * ============================================================================
 * AG-UI 어댑터 (교체 지점)
 * ============================================================================
 * 오케스트레이터(Task #4)가 내보내는 이벤트 스트림을 CopilotKit 이 이해하는
 * 표준 AG-UI `AbstractAgent` 로 감싼다. **이벤트 포맷이 바뀌면 이 파일만 바꾸면 된다.**
 *
 * 두 가지 어댑터:
 *  - 'agui'    : 오케스트레이터가 AGUIApp(표준 AG-UI SSE)를 노출하는 경우.
 *                @ag-ui/client 의 HttpAgent 가 SSE 파싱을 내부적으로 처리한다.
 *                (기본값 · orchestrator 는 ag_ui_strands 로 표준 이벤트 생성 확정)
 *  - 'raw-sse' : HttpAgent 통합에 문제가 생겼을 때의 폴백. RawSSEAgent 가 동일한 표준
 *                AG-UI SSE 를 직접 파싱해 통과시킨다. (ARCHITECTURE.md §8 리스크 완화)
 *
 * 두 어댑터 모두 SigV4 서명 fetch(sigv4Fetch)를 사용하므로 전송 계층은 동일하다.
 */

import { AbstractAgent, HttpAgent } from '@ag-ui/client';
import { sigv4Fetch } from './sigv4-fetch';
import { RawSSEAgent } from './raw-sse-agent';

export type AdapterKind = 'agui' | 'raw-sse';

export interface BuildAgentOptions {
  /** AgentCore /invocations 전체 URL (SigV4 서명 대상). */
  url: string;
  /** 요청에 항상 붙일 헤더 (예: 세션 ID, Accept: text/event-stream). */
  headers: Record<string, string>;
}

/**
 * 환경 변수 AGUI_ADAPTER 로 어댑터를 선택한다. 기본은 'agui'.
 */
export function resolveAdapterKind(): AdapterKind {
  const raw = (process.env.AGUI_ADAPTER ?? 'agui').toLowerCase();
  return raw === 'raw-sse' ? 'raw-sse' : 'agui';
}

/**
 * 선택된 어댑터에 맞는 AG-UI AbstractAgent 를 생성한다.
 * CopilotRuntime({ agents: { default: <이 반환값> } }) 로 넘긴다.
 */
export function buildAguiAgent(opts: BuildAgentOptions): AbstractAgent {
  const kind = resolveAdapterKind();

  if (kind === 'raw-sse') {
    return new RawSSEAgent({
      url: opts.url,
      headers: opts.headers,
      fetch: sigv4Fetch,
    });
  }

  // 표준 AG-UI: HttpAgent 가 SSE → BaseEvent 파싱을 담당.
  return new HttpAgent({
    url: opts.url,
    headers: opts.headers,
    fetch: sigv4Fetch,
  });
}
