// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useRef } from 'react';
import { Sparkles } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import type { ChatMessage } from './types';

const SUGGESTIONS = [
  '이번 달 비용 top 10 사용자',
  '어제 평소보다 비싸진 사용자 누구',
  'Claude Code vs Cowork 앱별 비용·모델 비교',
  '지난 30일 일별 총 비용 추이',
  '이번 달 80% 도달한 팀',
  '지난 24h 429 가장 많이 받은 사용자',
  '지난 30일 사용 패턴 outlier 사용자',
  '다음 달 총 비용 예측',
];

interface Props {
  messages: ChatMessage[];
  onSuggestionClick: (text: string) => void;
  sessionId?: string | null;
  mode?: 'quick' | 'deep';
}

export function MessageList({ messages, onSuggestionClick, sessionId, mode = 'quick' }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
          <Sparkles size={20} className="text-secondary-foreground" />
        </div>
        <h2 className="text-base font-semibold">{mode === 'deep' ? 'BI Insight' : 'Quick Chat'}</h2>
        <p className="mt-1 text-sm text-muted-foreground max-w-md">
          {mode === 'deep'
            ? '계획을 먼저 세우고 다단계 분석·교차 검증을 거쳐 신뢰할 수 있는 인사이트를 드립니다. 비용·사용량·이상치·예측 등 깊은 질문에 적합합니다.'
            : '자연어로 사용자 / 팀 / 예산 / 사용량 데이터를 빠르게 질의하세요. SQL은 자동 작성 + 검증되며, 분석/예측은 Python sandbox 에서 처리됩니다.'}
        </p>

        <div className="mt-8 grid grid-cols-1 gap-2 sm:grid-cols-2 max-w-2xl w-full">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onSuggestionClick(s)}
              className="rounded-md border border-border bg-card px-3 py-2.5 text-left text-sm hover:bg-accent transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    );
  }

  // 턴별 후속질문 칩(§55): 마지막 assistant 메시지의 suggestions 만 렌더
  // (이전 턴 칩은 숨김 — 대화가 진행되면 더 이상 유효하지 않을 수 있음).
  const last = messages[messages.length - 1];
  const followUps =
    last?.role === 'assistant' && !last.pending && last.suggestions?.length
      ? last.suggestions
      : null;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl">
        {messages.map((m, i) => (
          <MessageBubble
            key={m.id}
            message={m}
            sessionId={sessionId}
            // PlanCard [진행] — 마지막 assistant 메시지의 plan 에만 활성(§57).
            onPlanProceed={
              i === messages.length - 1 && m.plan && !m.pending
                ? () => onSuggestionClick('진행해줘')
                : undefined
            }
          />
        ))}
        {followUps && (
          <div className="flex flex-wrap items-center gap-2 px-4 pb-5 pt-1 pl-12">
            <span className="text-[11px] font-medium text-muted-foreground/70 mr-0.5">연관 질문</span>
            {followUps.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => onSuggestionClick(s)}
                className="rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs text-primary hover:bg-primary/10 transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {/* 스크롤 영역 바닥 여백 — 마지막 칩/버블이 입력창 상단 테두리에 붙지 않게. */}
        <div ref={endRef} className="h-3" />
      </div>
    </div>
  );
}
