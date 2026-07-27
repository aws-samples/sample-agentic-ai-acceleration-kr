// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { CopilotKitProvider } from '@copilotkit/react-core/v2';
import { getBrowserSessionId } from '@/lib/session';
import { toolCallRenderers } from './toolRenderers';

/**
 * CopilotKit(v2) 프로바이더. runtimeUrl 은 서버 사이드 프록시(/api/copilotkit)를 가리킨다.
 * headers 함수로 브라우저 세션 ID 를 매 요청에 전달 → 프록시가 AgentCore 세션 헤더로 재전달.
 * renderToolCalls 로 모든 TOOL_CALL_* 이벤트를 상태 칩·SQL·결과 표로 인라인 렌더.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <CopilotKitProvider
      runtimeUrl="/api/copilotkit"
      headers={() => ({ 'X-Session-Id': getBrowserSessionId() })}
      renderToolCalls={toolCallRenderers}
    >
      {children}
    </CopilotKitProvider>
  );
}
