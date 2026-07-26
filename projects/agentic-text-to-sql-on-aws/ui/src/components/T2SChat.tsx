// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useState } from 'react';
import { CopilotChat } from '@copilotkit/react-core/v2';
import { getBrowserSessionId } from '@/lib/session';
import { PipelineProgress } from './PipelineProgress';

/**
 * 채팅 본체(v2). CopilotKitProvider 하위에서 동작한다.
 * - 텍스트 스트리밍 델타: CopilotChat 이 TEXT_MESSAGE_* 를 자동 렌더(markdown 표 포함).
 * - 도구 진행/SQL: 프로바이더의 renderToolCalls(search_schema/run_sql)가 인라인 렌더.
 * - 파이프라인 진행(STEP_*): PipelineProgress 가 Graph 노드 진행을 상단에 표시.
 * - threadId 는 브라우저 세션 UUID 로 고정(AgentCore Memory 세션 격리 키).
 * - agentId 는 프록시의 CopilotRuntime({ agents: { text_to_sql } }) 와 일치해야 한다.
 */
export function T2SChat() {
  // threadId 는 클라이언트에서만 확정(SSR 플레이스홀더 방지).
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  useEffect(() => {
    setThreadId(getBrowserSessionId());
  }, []);

  return (
    <div className="t2s-chat-inner">
      <PipelineProgress agentId="text_to_sql" />
      <CopilotChat
        agentId="text_to_sql"
        threadId={threadId}
        labels={{
          chatInputPlaceholder: '질문을 입력하세요…',
          welcomeMessageText:
            '안녕하세요! 자연어로 데이터를 질의해 보세요. 예) "지난달 지역별 매출 상위 5개를 보여줘"',
        }}
      />
    </div>
  );
}
