// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useRef, useState } from 'react';
import { useAgent, useCopilotKit } from '@copilotkit/react-core/v2';
import {
  ClarificationForm,
  type ClarificationField,
  type ClarificationRequest,
  type ClarificationValues,
} from './ClarificationForm';

/**
 * ============================================================================
 * ClarificationHost — clarification 이벤트 수신 + 재실행 트리거 (M2)
 * ============================================================================
 * 흐름(확정 계약):
 *  1. orchestrator 가 정보 부족 시 CUSTOM 이벤트(name="clarification_request")를 방출하고
 *     RUN_FINISHED 로 스트림을 닫는다.
 *  2. 이 호스트가 `useAgent().agent.subscribe({ onCustomEvent })` 로 이벤트를 수신
 *     (@ag-ui/client 0.0.57 AgentSubscriber 네이티브 훅). value 스키마를 폼으로 렌더.
 *  3. 사용자가 제출하면 **동일 threadId 로 재실행** — `copilotkit.runAgent({ agent, forwardedProps })`
 *     로 forwardedProps.clarificationResponse = { interruptId, values } 를 전달한다.
 *
 * forwardedProps 전달 방식 근거(설치본 타입/소스 확인):
 *  - CopilotKitCore.runAgent({ agent, forwardedProps }) 가 per-run forwardedProps 를
 *    provider properties 와 병합해 agent.runAgent 입력에 싣는다(@copilotkit/core runAgent).
 *  - AbstractAgent.prepareRunAgentInput 이 forwardedProps 를 RunAgentInput 에 포함하고
 *    HttpAgent 가 전체 입력을 프록시 요청 본문으로 직렬화한다(@ag-ui/client).
 *  - v2 런타임(handle-run→in-memory runner)이 input 을 백엔드 에이전트에 그대로 전달하므로
 *    forwardedProps 가 AgentCore 까지 도달한다. → 헤더/route.ts 우회가 불필요한 가장 견고한 경로.
 */

/** CUSTOM 이벤트 이름 (확정 계약). */
const CLARIFICATION_EVENT = 'clarification_request';
const FIELD_TYPES = new Set(['select', 'date_range', 'text']);

/** CUSTOM 이벤트 value(unknown) → ClarificationRequest 정규화. 유효하지 않으면 null. */
function parseClarification(value: unknown): ClarificationRequest | null {
  if (!value || typeof value !== 'object') return null;
  const v = value as Record<string, unknown>;
  const interruptId = typeof v.interruptId === 'string' ? v.interruptId : null;
  const question = typeof v.question === 'string' ? v.question : null;
  if (!interruptId || !question) return null;

  const rawFields = Array.isArray(v.fields) ? v.fields : [];
  const fields: ClarificationField[] = [];
  for (const raw of rawFields) {
    if (!raw || typeof raw !== 'object') continue;
    const f = raw as Record<string, unknown>;
    const name = typeof f.name === 'string' ? f.name : null;
    const type = typeof f.type === 'string' && FIELD_TYPES.has(f.type) ? f.type : null;
    if (!name || !type) continue;
    fields.push({
      name,
      label: typeof f.label === 'string' ? f.label : name,
      type: type as ClarificationField['type'],
      options: Array.isArray(f.options)
        ? f.options.filter((o): o is string => typeof o === 'string')
        : undefined,
    });
  }
  if (fields.length === 0) return null;

  return {
    interruptId,
    interruptName: typeof v.interruptName === 'string' ? v.interruptName : undefined,
    question,
    fields,
  };
}

/** 제출 값 → 채팅에 남길 사용자 메시지 요약(가독성·추적성). */
function summarize(request: ClarificationRequest, values: ClarificationValues): string {
  const parts: string[] = [];
  for (const field of request.fields) {
    const val = values[field.name];
    if (val === undefined) continue;
    if (typeof val === 'string') {
      parts.push(`${field.label}: ${val}`);
    } else {
      const range = [val.from, val.to].filter(Boolean).join(' ~ ');
      if (range) parts.push(`${field.label}: ${range}`);
    }
  }
  return parts.length > 0 ? `(재요청 응답: ${parts.join(', ')})` : '(재요청 응답: 최선 추정으로 진행)';
}

export function ClarificationHost({ agentId }: { agentId: string }) {
  const { agent } = useAgent({ agentId });
  const { copilotkit } = useCopilotKit();

  const [request, setRequest] = useState<ClarificationRequest | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 같은 run 에서 이미 표시한 interruptId 중복 수신 방어.
  const handledRef = useRef<string | null>(null);

  useEffect(() => {
    if (!agent) return;
    const sub = agent.subscribe({
      onRunStartedEvent: () => {
        // 새 run 시작 → 이전 폼 정리(제출로 인한 재실행 포함).
        setRequest(null);
        setSubmitting(false);
        handledRef.current = null;
      },
      onCustomEvent: ({ event }) => {
        if (event.name !== CLARIFICATION_EVENT) return;
        const parsed = parseClarification(event.value);
        if (!parsed) return;
        if (handledRef.current === parsed.interruptId) return; // 중복 방어
        handledRef.current = parsed.interruptId;
        setRequest(parsed);
        setSubmitting(false);
      },
    });
    return () => sub.unsubscribe();
  }, [agent]);

  if (!request) return null;

  const rerun = (values: ClarificationValues) => {
    if (submitting) return;
    setSubmitting(true);
    // 채팅 히스토리에 사용자 응답을 남긴다(재실행 입력에 포함 + 가독성).
    agent.addMessage({
      id: crypto.randomUUID(),
      role: 'user',
      content: summarize(request, values),
    });
    // 동일 threadId 재실행 + forwardedProps.clarificationResponse 전달.
    void copilotkit.runAgent({
      agent,
      forwardedProps: {
        clarificationResponse: { interruptId: request.interruptId, values },
      },
    });
    // RUN_STARTED 수신 시 폼이 정리되지만, 즉시 숨겨 재제출을 방지한다.
    setRequest(null);
  };

  return (
    <div className="t2s-clarify-wrap">
      <ClarificationForm
        request={request}
        disabled={submitting}
        onSubmit={(values) => rerun(values)}
        onSkip={() => rerun({})}
      />
    </div>
  );
}
