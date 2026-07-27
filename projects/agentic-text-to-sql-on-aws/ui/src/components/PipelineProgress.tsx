// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useState } from 'react';
import { useAgent } from '@copilotkit/react-core/v2';

/**
 * 파이프라인 진행 표시 (orchestrator 확정 계약 기준).
 * orchestrator 는 Graph 노드 전이마다 STEP_STARTED/STEP_FINISHED {stepName} 을 방출한다.
 * TOOL_CALL_* 보다 이 이벤트가 파이프라인 진행의 정확한 소스이므로 상단 진행 바에 사용한다.
 *
 * @ag-ui/client 0.0.57 은 STEP_* 타입과 AgentSubscriber.onStepStarted/FinishedEvent 를
 * 네이티브 지원한다. useAgent().agent 에 구독해 stepName 을 한국어 라벨로 렌더.
 */

// Graph 노드명 → 한국어 라벨 (확정: intent, schema_linking, sql_generation, execution, synthesis)
const STEP_LABELS: Record<string, string> = {
  intent: '의도 분석',
  schema_linking: '스키마 연결',
  sql_generation: 'SQL 생성',
  execution: '실행',
  synthesis: '결과 정리',
};

// 표시 순서 (진행 바 정렬용).
const STEP_ORDER = ['intent', 'schema_linking', 'sql_generation', 'execution', 'synthesis'];

type StepState = 'pending' | 'active' | 'done';

export function PipelineProgress({ agentId }: { agentId: string }) {
  const { agent } = useAgent({ agentId });
  const [states, setStates] = useState<Record<string, StepState>>({});
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!agent) return;
    const sub = agent.subscribe({
      onRunStartedEvent: () => {
        setStates({});
        setVisible(true);
      },
      onStepStartedEvent: ({ event }) => {
        const name = event.stepName;
        if (!name) return;
        setStates((prev) => ({ ...prev, [name]: 'active' }));
      },
      onStepFinishedEvent: ({ event }) => {
        const name = event.stepName;
        if (!name) return;
        setStates((prev) => ({ ...prev, [name]: 'done' }));
      },
      // 실행 종료 시 진행 바는 남겨두되(완료 상태), 다음 run 시작 때 초기화.
    });
    return () => sub.unsubscribe();
  }, [agent]);

  if (!visible) return null;

  // 확정 순서 + 혹시 모르는 미등록 stepName 도 뒤에 붙여 관측.
  const knownSteps = STEP_ORDER.filter((s) => s in states || true);
  const extraSteps = Object.keys(states).filter((s) => !STEP_ORDER.includes(s));
  const steps = [...knownSteps, ...extraSteps];

  return (
    <div className="t2s-pipeline" role="status" aria-label="파이프라인 진행 상황">
      {steps.map((step) => {
        const state = states[step] ?? 'pending';
        const label = STEP_LABELS[step] ?? step;
        return (
          <div key={step} className={`t2s-step t2s-step-${state}`}>
            <span className="t2s-step-dot" aria-hidden>
              {state === 'done' ? '✓' : state === 'active' ? '' : ''}
            </span>
            <span className="t2s-step-label">{label}</span>
          </div>
        );
      })}
    </div>
  );
}
