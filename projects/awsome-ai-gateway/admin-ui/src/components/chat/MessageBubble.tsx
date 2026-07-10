// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useRef, useState } from 'react';
import { Sparkles, User, ChevronDown, ChevronRight, Brain, FileDown } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatMessage } from './types';
import { ChartRenderer } from './ChartRenderer';
import { ToolCallBlock } from './ToolCallBlock';
import { HeartbeatTimeline } from './HeartbeatTimeline';
import { ReportCard } from './ReportCard';
import { PlanCard } from './PlanCard';
import { exportMessageReport } from './reportExport';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 답변 본문의 마크다운 표를 모델관리 등과 동일한 공통 Table 컴포넌트로 렌더(§60) —
// 채팅·관리 화면 테이블 디자인 통일. GFM 정렬(:---:/---:)이 style.textAlign='right'
// 로 들어오면 숫자 컬럼으로 보고 numeric(우측정렬+tabular-nums) 적용.
const isRightAligned = (style?: React.CSSProperties): boolean =>
  (style as { textAlign?: string } | undefined)?.textAlign === 'right';

const MD_COMPONENTS: Components = {
  table: ({ children }) => (
    <div className="my-2">
      <Table density="compact">{children}</Table>
    </div>
  ),
  thead: ({ children }) => <THead>{children}</THead>,
  tbody: ({ children }) => <TBody>{children}</TBody>,
  tr: ({ children }) => <Tr>{children}</Tr>,
  th: ({ children, style }) => <Th numeric={isRightAligned(style)}>{children}</Th>,
  td: ({ children, style }) => <Td numeric={isRightAligned(style)}>{children}</Td>,
};

interface Props {
  message: ChatMessage;
  /** PlanCard [진행] 버튼 → "진행해줘" 전송(§57). 마지막 메시지에서만 전달됨. */
  onPlanProceed?: () => void;
  /** 인라인 SQL 재실행용 세션ID(§57). */
  sessionId?: string | null;
}

/**
 * 진행 표시 — bouncing dots + 단계 라벨 + **경과 초 카운터**.
 * 5-agent 파이프라인은 단순 SQL 도 ~60초, Code 분석은 수 분 걸린다. 정적 "분석 중…"
 * 은 "멈춤" 으로 오인되므로, 1초마다 갱신되는 경과 시간으로 "살아있음" 을 보인다.
 * label 은 tool_call 단계에 따라 갱신(ChatLayout.applyEvent → thinkingText).
 */
function PendingIndicator({ label }: { label: string }) {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef<number>(Date.now());
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <span className="flex gap-1">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground" />
      </span>
      <span>{label}</span>
      {elapsed >= 3 && (
        <span className="text-muted-foreground/60 tabular-nums">{elapsed}초</span>
      )}
    </div>
  );
}

/**
 * 추론 스트림("사고 과정") — orchestrator display:summarized 의 추론 요약 델타.
 * 침묵 구간을 연속 텍스트로 메운다. 스트리밍 중(pending)엔 자동 펼침(실시간 진행
 * 표시), 완료 후엔 접어 답변에 집중. 답변(content)과 분리된 별도 영역.
 */
function ReasoningBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(true);
  // 완료되면 자동 접기(스트리밍 끝난 직후 1회). live→false 전환 시.
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) setOpen(false);
    wasLive.current = live;
  }, [live]);
  return (
    <div className="mb-2 rounded-md border border-border/60 bg-muted/20">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-accent/40 transition-colors"
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Brain size={13} className={live ? 'animate-pulse' : ''} />
        <span>{live ? '사고 과정 (분석 중…)' : '사고 과정'}</span>
      </button>
      {open && (
        <div className="border-t border-border/60 px-3 py-2 text-xs leading-relaxed text-muted-foreground/90 whitespace-pre-wrap break-words max-h-60 overflow-auto">
          {text}
          {live && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-muted-foreground align-middle" />}
        </div>
      )}
    </div>
  );
}

export function MessageBubble({ message, onPlanProceed, sessionId }: Props) {
  const isUser = message.role === 'user';
  const bodyRef = useRef<HTMLDivElement>(null);

  // 리포트 가능 조건: 분석이 끝난(assistant·non-pending) 메시지가 **실질 분석 결과**를
  // 가질 때 — 차트가 있거나, 본문이 있되 계획(plan)-only 카드가 아닐 때. plan-first
  // 카드([진행] 대기)에는 아직 결과가 없으므로 버튼을 숨긴다. 화면의 narrative +
  // recharts SVG + 표 + SQL 을 자체완결 HTML 리포트(인쇄→PDF)로 내보낸다(서버 왕복 없음).
  const canExport =
    !isUser &&
    !message.pending &&
    ((message.charts?.length ?? 0) > 0 || (!!message.content?.trim() && !message.plan));

  function handleExport() {
    if (!bodyRef.current) return;
    const title = deriveReportTitle(message);
    exportMessageReport(bodyRef.current, message, title);
  }

  return (
    <div
      className={[
        'flex gap-3 px-4 py-5 border-b border-border last:border-b-0',
        isUser ? 'bg-muted/40' : 'bg-transparent',
      ].join(' ')}
    >
      <div className="flex-shrink-0 mt-1">
        <div
          className={[
            'flex h-8 w-8 items-center justify-center rounded-full',
            isUser ? 'bg-primary text-primary-foreground' : 'bg-secondary text-secondary-foreground',
          ].join(' ')}
        >
          {isUser ? <User size={14} /> : <Sparkles size={14} />}
        </div>
      </div>

      <div className="flex-1 min-w-0" ref={bodyRef}>
        <div className="text-xs font-medium text-muted-foreground mb-1.5">
          {isUser ? '나' : 'Admin Chat'}
          {message.createdAt && (
            <span className="ml-2 text-muted-foreground/70">
              {new Date(message.createdAt).toLocaleTimeString('ko-KR', {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          )}
        </div>

        {/* 추론 스트림 — 답변보다 먼저(추론이 답변에 선행). 스트리밍 중 자동 펼침. */}
        {message.reasoning && (
          <ReasoningBlock text={message.reasoning} live={!!message.pending && !message.content} />
        )}

        {message.content && (
          <div
            data-report-narrative
            className="text-sm text-foreground leading-relaxed [&_p]:my-1.5 [&_ul]:my-1.5 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px] [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3 [&_a]:text-primary [&_a]:underline [&_strong]:font-semibold [&_h1]:text-base [&_h1]:font-semibold [&_h1]:mt-2 [&_h2]:text-sm [&_h2]:font-semibold [&_h2]:mt-2 [&_h3]:text-sm [&_h3]:font-semibold"
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{message.content}</ReactMarkdown>
            {message.pending && (
              <span className="ml-1 inline-block h-4 w-2 animate-pulse bg-foreground align-middle" />
            )}
          </div>
        )}

        {/* 진행 표시. 본문 시작 전 + heartbeat 타임라인이 있으면 "화려한" 타임라인,
            없으면(초기/폴백) 단순 PendingIndicator. 본문 도착 후엔 타임라인을 거두고
            가벼운 인디케이터만(타이머 누수 방지 — 타임라인 interval 도 함께 사라짐). */}
        {message.pending && (
          <div className={message.content ? 'mt-2' : ''}>
            {!message.content && message.heartbeats && message.heartbeats.length > 0 ? (
              <HeartbeatTimeline phases={message.heartbeats} heartbeatAt={message.heartbeatAt} />
            ) : (
              <PendingIndicator label={message.thinkingText || '분석 중…'} />
            )}
          </div>
        )}

        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mt-3 flex flex-col gap-2">
            {message.toolCalls.map((tc, i) => (
              <ToolCallBlock key={i} toolCall={tc} sessionId={sessionId} />
            ))}
          </div>
        )}

        {message.validator && message.validator.verdict !== 'PASS' && (
          <div
            className={[
              'mt-3 rounded-md border px-3 py-2 text-xs',
              // 10% 틴트 배경 위에는 saturate된 중간톤 텍스트를 써야 읽힌다 — *-foreground
              // 토큰(흰색)은 solid 버튼용이라 여기선 안 보였다(아래 emerald 검증 카드와 동일 패턴).
              message.validator.verdict === 'WARN'
                ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                : 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
            ].join(' ')}
          >
            <span className="font-semibold">
              {message.validator.verdict === 'WARN' ? '⚠ 검증 경고' : '✗ 검증 실패'}
            </span>
            <span className="ml-2">{message.validator.reason}</span>
          </div>
        )}

        {message.verifications && message.verifications.length > 0 && (
          <div className="mt-3 flex flex-col gap-1.5">
            {message.verifications.map((v, i) => {
              const pct = Math.round((v.agreement || 0) * 100);
              const ok = v.verdict === 'PASS';
              return (
                <div
                  key={i}
                  className={[
                    'rounded-md border px-3 py-2 text-xs',
                    ok
                      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                      : 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
                  ].join(' ')}
                  title={v.chosen_sql || ''}
                >
                  <span className="font-semibold">
                    {ok ? '✓ 실행 검증됨' : '⚠ 검증 주의'}
                  </span>
                  <span className="ml-2">
                    후보 {v.n_valid}/{v.k}개 실행 · 결과 합의 {pct}%
                    {v.tie ? ' · 결과 갈림(수동 확인 권장)' : ''}
                  </span>
                </div>
              );
            })}
          </div>
        )}

        {message.audit && message.audit.verdict !== 'PASS' && (
          <div
            className={[
              'mt-3 rounded-md border px-3 py-2 text-xs',
              // RETRY=재검증 권장(amber), NEEDS_REVIEW=사람 검토 필요(red). 검증카드와 동일 톤.
              message.audit.verdict === 'RETRY'
                ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                : 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
            ].join(' ')}
          >
            <span className="font-semibold">
              {message.audit.verdict === 'RETRY' ? '⚠ 답변 재검증 권장' : '⚠ 수치 검토 필요'}
            </span>
            <span className="ml-2">{message.audit.reason}</span>
            {message.audit.defects && message.audit.defects.length > 0 && (
              <ul className="mt-1.5 space-y-1 border-l-2 border-current/20 pl-2 text-[11px] opacity-90">
                {message.audit.defects.slice(0, 3).map((d, i) => (
                  <li key={i}>
                    {d.body_excerpt && <span className="font-mono">“{d.body_excerpt}”</span>}
                    {d.body_value !== undefined && (
                      <span className="ml-1">
                        ({d.body_value}
                        {d.ground_values && d.ground_values.length > 0 && ` ≠ 실행값 ${d.ground_values[0]}`})
                      </span>
                    )}
                    {d.suggested_fix && <span className="ml-1 italic opacity-80">→ {d.suggested_fix}</span>}
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-1.5 text-[10px] opacity-70">
              독립 감사{message.audit.model ? ` · ${message.audit.model}` : ''} · 수치는 그대로 두고 경고만 표시
            </div>
          </div>
        )}

        {message.plan && (
          <PlanCard
            plan={message.plan}
            onProceed={onPlanProceed}
            disabled={!onPlanProceed}
          />
        )}

        {message.charts && message.charts.length > 0 && (
          <div className="mt-4 flex flex-col gap-3">
            {message.charts.map((spec, i) => (
              <ChartRenderer key={i} spec={spec} />
            ))}
          </div>
        )}

        {message.reports && message.reports.length > 0 && (
          <div className="flex flex-col gap-2">
            {message.reports.map((r, i) => (
              <ReportCard key={i} report={r} />
            ))}
          </div>
        )}

        {/* 분석 결과 → 보고서 다운로드(서술 본문 + recharts 벡터 차트 + 데이터 표 +
            실행 SQL 부록). 화면에 보이는 것을 그대로 자체완결 HTML 로 모아 인쇄(PDF
            저장)한다 — 서버 왕복·데이터 재집계 없음, SVG 무손실 보존. */}
        {(canExport || message.costUsd || message.durationMs) && (
          <div className="mt-3 flex items-center gap-3">
            {(message.costUsd || message.durationMs) && (
              <div className="text-[11px] text-muted-foreground/80">
                {message.costUsd && <span>비용 ${message.costUsd.toFixed(4)}</span>}
                {message.costUsd && message.durationMs && <span className="mx-2">·</span>}
                {message.durationMs && <span>{message.durationMs}ms</span>}
              </div>
            )}
            {canExport && (
              <button
                type="button"
                onClick={handleExport}
                className="ml-auto flex items-center gap-1.5 rounded-md border border-border bg-card/50 px-2.5 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
                title="이 분석을 서술 + 차트 포함 보고서로 다운로드(인쇄 → PDF로 저장)"
              >
                <FileDown size={13} />
                보고서 다운로드
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// 리포트 제목: 차트 제목 → 본문 첫 헤딩/문장 → 기본값. 파일명·문서 제목에 쓰인다.
function deriveReportTitle(message: ChatMessage): string {
  const chartTitle = message.charts?.find((c) => c.title)?.title;
  if (chartTitle) return chartTitle;
  const text = message.content?.trim();
  if (text) {
    // 첫 마크다운 헤딩(#...) 또는 첫 문장/줄.
    const heading = text.match(/^#{1,3}\s+(.+)$/m)?.[1];
    const first = heading || text.split('\n')[0].replace(/[#*`>]/g, '').trim();
    if (first) return first.length > 60 ? `${first.slice(0, 60)}…` : first;
  }
  return 'BI Insight 분석 리포트';
}
