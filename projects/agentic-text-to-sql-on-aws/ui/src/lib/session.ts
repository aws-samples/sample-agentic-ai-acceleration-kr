// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

const SESSION_STORAGE_KEY = 'agentic-t2s-session-id';

/**
 * 브라우저 세션당 UUID 를 생성/재사용한다. 이 값은 CopilotKit 요청 헤더(X-Session-Id)로
 * 프록시에 전달되고, 프록시가 AgentCore 세션 헤더로 재전달해 동일 microVM 라우팅을 유지한다.
 *
 * ⚠️ AgentCore 세션 affinity 는 워크플로 상태를 자동 복원하지 않는다. 재개 정확성은
 * 오케스트레이터의 AgentCoreMemorySessionManager 로 보장된다 (docs/architecture.md §4.1).
 */
export function getBrowserSessionId(): string {
  if (typeof window === 'undefined') {
    // SSR 단계에서는 임시 값 (실제 헤더는 클라이언트에서 확정).
    return 'ssr-placeholder';
  }
  let id = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!id) {
    id = crypto.randomUUID();
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, id);
  }
  return id;
}
