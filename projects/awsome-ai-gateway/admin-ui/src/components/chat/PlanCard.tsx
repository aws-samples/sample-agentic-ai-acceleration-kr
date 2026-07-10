// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { ClipboardList, Database, Code2, ShieldCheck, BarChart3, Play } from 'lucide-react';
import type { AnalysisPlan } from './types';

/**
 * deep 모드 분석 계획 카드(§57 — deep-insight HITL 차용, 전달은 대화 턴).
 *
 * orchestrator_deep 의 plan-first(```plan 펜스)가 plan 이벤트로 구조화되어 오면
 * raw JSON 대신 단계 체크리스트 카드로 렌더. [진행] 버튼 = "진행해줘" 일반 메시지
 * 전송(별도 피드백 채널 없음 — AgentCore 세션이 대화 이력을 보존). 수정은
 * 입력창에 자유 텍스트로(카드가 안내).
 */

const TOOL_META: Record<string, { icon: typeof Database; label: string }> = {
  sql: { icon: Database, label: 'SQL' },
  code: { icon: Code2, label: 'Python' },
  validate: { icon: ShieldCheck, label: '검증' },
  viz: { icon: BarChart3, label: '차트' },
};

export function PlanCard({
  plan,
  onProceed,
  disabled,
}: {
  plan: AnalysisPlan;
  onProceed?: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="my-2 rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
        <ClipboardList size={15} className="text-primary" />
        {plan.title || '분석 계획'}
      </div>
      <ol className="flex flex-col gap-1.5">
        {plan.steps.map((s, i) => {
          const meta = TOOL_META[s.tool || ''] || null;
          const Icon = meta?.icon;
          return (
            <li key={s.id ?? i} className="flex items-start gap-2 text-sm">
              <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-medium text-primary">
                {s.id ?? i + 1}
              </span>
              <span className="flex-1 text-foreground/90">{s.label}</span>
              {meta && Icon && (
                <span className="mt-0.5 flex flex-shrink-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                  <Icon size={11} />
                  {meta.label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
      {onProceed && (
        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            onClick={onProceed}
            disabled={disabled}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
          >
            <Play size={12} />
            이 계획으로 진행
          </button>
          <span className="text-[11px] text-muted-foreground">
            수정하려면 입력창에 변경 내용을 적어주세요 (예: &quot;2단계는 빼고&quot;)
          </span>
        </div>
      )}
    </div>
  );
}
