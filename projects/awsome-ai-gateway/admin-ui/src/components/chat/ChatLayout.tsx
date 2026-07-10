// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Send, Loader2, StopCircle, MessageSquarePlus } from 'lucide-react';
import { createSession, getHistory } from './api';
import { MessageList } from './MessageList';
import { useChatStream } from './useChatStream';
import { useChatPanelOptional } from './ChatProvider';
import type { ChatMessage, ChartSpec, ValidatorResult, VerificationResult, AuditResult } from './types';

export interface ChatLayoutProps {
  /**
   * page: /chat 풀페이지 (viewport 높이 고정).
   * drawer: FAB 드로어 안 (부모 높이를 채움 h-full).
   */
  variant?: 'page' | 'drawer';
  /**
   * quick: 즉답형(퀵챗 기본). deep: plan-first 심층분석(사이드바 Chat) —
   * agent 가 orchestrator 프로필을 바꾼다(§55). 미지정 시 quick.
   */
  mode?: 'quick' | 'deep';
}

export function ChatLayout({ variant = 'page', mode = 'quick' }: ChatLayoutProps) {
  // 분할뷰 패널 안에서 쓰일 땐 "지금 보는 화면" 컨텍스트를 동봉. Provider 밖
  // (/chat 풀페이지)이면 null → 컨텍스트 없이 기존 동작.
  const chatPanel = useChatPanelOptional();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const initRef = useRef(false);

  // 첫 마운트(§56 핸드오프): deep(/chat 페이지)은 최근 세션을 복원해 다른 메뉴에
  // 다녀와도 대화·결과가 유지된다 — sessionStorage 의 세션ID 로 history 로드 후,
  // 진행 중 분석이 있으면 GET /stream 으로 재구독(서버가 background 로 계속 돌림).
  // 복원 실패/만료 시 새 세션 폴백. 퀵챗(drawer)은 DOM 유지 방식이라 기존 그대로.
  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    const storeKey = `chat-session-${mode}`;
    const saved =
      typeof window !== 'undefined' ? sessionStorage.getItem(storeKey) : null;

    const startFresh = () =>
      createSession()
        .then((s) => {
          sessionStorage.setItem(storeKey, s.session_id);
          setSessionId(s.session_id);
        })
        .catch((e) => setError(`세션 생성 실패: ${e.message}`));

    if (!saved) {
      startFresh();
      return;
    }
    // 저장된 세션 복원: history 로드 → (진행 중이면) 라이브 스트림 재구독
    getHistory(saved)
      .then((msgs) => {
        setSessionId(saved);
        setMessages(msgs);
        reattach(saved); // 진행 중 분석 있으면 이어서 수신(404 면 무시)
      })
      .catch(() => {
        sessionStorage.removeItem(storeKey);
        startFresh();
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  const handleEvent = useCallback((event: any) => {
    setMessages((prev) => {
      // 마지막 assistant 메시지를 in-place 갱신
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant' || !last.pending) {
        // 새 assistant 메시지 시작
        const next: ChatMessage = {
          id: `pending-${Date.now()}`,
          role: 'assistant',
          content: '',
          pending: true,
          createdAt: new Date().toISOString(),
        };
        return [...prev, applyEvent(next, event)];
      }
      const updated = applyEvent(last, event);
      return [...prev.slice(0, -1), updated];
    });
  }, []);

  const handleError = useCallback((e: Error) => {
    setError(e.message);
  }, []);

  const { send, cancel, reattach, isStreaming } = useChatStream({
    onEvent: handleEvent,
    onError: handleError,
  });

  const handleSubmit = useCallback(
    async (text: string) => {
      if (!text.trim() || !sessionId || isStreaming) return;
      setError(null);

      const userMsg: ChatMessage = {
        id: `u-${Date.now()}`,
        role: 'user',
        content: text,
        createdAt: new Date().toISOString(),
      };
      // 낙관적 pending 버블 — 첫 SSE 이벤트(네트워크+프록시 경유 ~0.5s+)를
      // 기다리지 않고 전송 즉시 "분석 중…" 표시. handleEvent 는 마지막 pending
      // assistant 버블을 in-place 갱신하므로 이 버블이 그대로 채워진다(§53).
      const pendingMsg: ChatMessage = {
        id: `pending-${Date.now()}`,
        role: 'assistant',
        content: '',
        pending: true,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg, pendingMsg]);
      setInput('');

      // 첫 메시지에만 화면 컨텍스트 동봉(이후 같은 세션은 대화 맥락으로 충분).
      const isFirst = messages.filter((m) => m.role === 'user').length === 0;
      const screenCtx = isFirst ? chatPanel?.screenContext ?? undefined : undefined;
      await send(sessionId, text, screenCtx, mode);

      // streaming 완료 → pending 플래그 제거
      setMessages((prev) =>
        prev.map((m) => (m.pending ? { ...m, pending: false } : m))
      );
    },
    [sessionId, isStreaming, send, messages, chatPanel, mode]
  );

  // "새 대화" — 새 세션 생성 + sessionStorage 교체 + 화면 비우기. 기존 대화는
  // 서버(DB)에 남아 파괴되지 않음(목록에서 복원 가능). 스트리밍 중엔 막는다.
  const handleNewChat = useCallback(async () => {
    if (isStreaming) return;
    setError(null);
    try {
      const s = await createSession();
      if (typeof window !== 'undefined') {
        sessionStorage.setItem(`chat-session-${mode}`, s.session_id);
      }
      setSessionId(s.session_id);
      setMessages([]);
      setInput('');
    } catch (e) {
      setError(`새 대화 생성 실패: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [isStreaming, mode]);

  const isDrawer = variant === 'drawer';

  return (
    <div
      className={
        isDrawer
          ? 'flex h-full flex-col bg-background overflow-hidden'
          : // page variant: 부모 <main>(flex-1, p-6)이 이미 헤더 아래 잔여공간을
            // 정확히 차지한다. 과거 h-[calc(100vh-3rem)]는 헤더(64px)+main 패딩을
            // 무시해 입력창이 화면 밖 27px 로 밀려 스크롤해야 보였다 → h-full 로
            // 부모 콘텐츠박스에 정확히 맞춘다(헤더/패딩 높이에 무관).
            'flex h-full flex-col rounded-lg border border-border bg-background overflow-hidden'
      }
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-base font-semibold">
              {mode === 'deep' ? 'BI Insight' : 'Quick Chat'}
            </h1>
            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
              {mode === 'deep' ? '심층 분석' : '즉답'}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {mode === 'deep'
              ? '계획 수립 → 다단계 분석 → 교차 검증 → 인사이트 · 5-agent powered by Bedrock Claude'
              : '5-agent (Orchestrator + SQL + Code + Validator + Viz) · powered by Bedrock Claude'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {sessionId && messages.length > 0 && (
            <button
              type="button"
              onClick={handleNewChat}
              disabled={isStreaming}
              title="새 대화 시작 (기존 대화는 보존됩니다)"
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <MessageSquarePlus size={14} />
              새 대화
            </button>
          )}
          {sessionId && (
            <span className="text-[11px] text-muted-foreground/80 font-mono">
              session {sessionId.slice(0, 8)}
            </span>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-hidden">
        <MessageList messages={messages} sessionId={sessionId} mode={mode} onSuggestionClick={(s) => handleSubmit(s)} />
      </div>

      {error && (
        <div className="border-t border-destructive bg-destructive/10 px-4 py-2 text-xs text-destructive-foreground">
          {error}
        </div>
      )}

      <form
        className="border-t border-border bg-background px-4 py-3"
        onSubmit={(e) => {
          e.preventDefault();
          handleSubmit(input);
        }}
      >
        <div className="flex gap-2 items-end mx-auto max-w-4xl">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // 한글 IME 조합 중 Enter 는 '조합 확정'이지 전송이 아니다.
              // isComposing 체크 안 하면 마지막 글자가 input 에 commit 되기 전에
              // 전송+clear 되고, 직후 조합 확정 onChange 가 빈 입력창에 그 글자를
              // 다시 박는다(= "마지막 글자 남음" 버그). keyCode 229 는 구형 폴백.
              if (
                e.key === 'Enter' &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing &&
                e.keyCode !== 229
              ) {
                e.preventDefault();
                handleSubmit(input);
              }
            }}
            placeholder="자연어로 질문하세요. Enter 로 전송, Shift+Enter 로 줄바꿈."
            rows={2}
            disabled={!sessionId}
            className="flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={cancel}
              className="flex h-10 w-10 items-center justify-center rounded-md border border-border bg-card hover:bg-accent transition-colors"
              aria-label="취소"
            >
              <StopCircle size={16} />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim() || !sessionId}
              className="flex h-10 w-10 items-center justify-center rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              aria-label="전송"
            >
              {sessionId ? <Send size={16} /> : <Loader2 size={16} className="animate-spin" />}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

// tool_call 시 "지금 무엇을 하는지" 진전 라벨. 침묵 구간(각 sub-agent LLM 이
// 도는 12~25초)에 정적 "분석 중…" 대신 단계별 안내를 보여 "멈춤" 인상 해소.
const TOOL_PROGRESS: Record<string, string> = {
  ask_sql_specialist: 'SQL 생성 중…',
  ask_code_specialist: '데이터 분석(Python) 중…',
  ask_validator: 'SQL 검증 중…',
  ask_viz_specialist: '차트 구성 중…',
  query_db: 'DB 조회 중…',
  get_schema: '스키마 확인 중…',
  render_chart: '차트 생성 중…',
  execute_python: 'Python 실행 중…',
};

function applyEvent(msg: ChatMessage, event: any): ChatMessage {
  switch (event.type) {
    case 'thinking':
      // 첫 토큰 전 "작업 중" 표시. 본문이 이미 있으면 무시(잡음 방지).
      return msg.content ? msg : { ...msg, thinkingText: event.text || '분석 중…' };
    case 'heartbeat': {
      // 공백 없는 스트리밍 생존신호 → 진행 타임라인. 본문이 이미 시작했으면 무시
      // (서버도 멈추지만 늦게 도착한 프레임 방어). 같은 phase 연속이면 count 증가
      // (SQL 재시도 등 비단조 경로), 다른 phase 면 새 단계 push.
      if (msg.content) return msg;
      const hbEvent = event as { phase: string; label: string; elapsed_ms: number };
      const prev = msg.heartbeats || [];
      const last = prev[prev.length - 1];
      let heartbeats: typeof prev;
      if (last && last.phase === hbEvent.phase) {
        heartbeats = [...prev.slice(0, -1), { ...last, count: last.count + 1, elapsedMs: hbEvent.elapsed_ms }];
      } else {
        heartbeats = [
          ...prev,
          { phase: hbEvent.phase, label: hbEvent.label, elapsedMs: hbEvent.elapsed_ms, count: 1 },
        ];
      }
      return { ...msg, heartbeats, heartbeatAt: Date.now() };
    }
    case 'reasoning':
      // 추론 요약 델타 누적 — 침묵 구간을 메우는 연속 "사고 과정" 스트림.
      // 답변(content)과 분리해 별도 영역에 표시.
      return { ...msg, reasoning: (msg.reasoning || '') + (event.chunk || '') };
    case 'tool_call':
      // 진전 라벨로 thinkingText 갱신 — 본문이 있어도 갱신(다음 단계로 넘어갔음을
      // 알림). MessageBubble 은 본문 위 진행줄에 이 라벨을 표시.
      return {
        ...msg,
        thinkingText: TOOL_PROGRESS[event.tool] || '처리 중…',
        toolCalls: [
          ...(msg.toolCalls || []),
          { tool: event.tool, args: event.args, status: 'running' as const },
        ],
      };
    case 'tool_result': {
      const calls = [...(msg.toolCalls || [])];
      // 같은 이름 중 result 없는 첫 항목에 채움(§57 fix — 끝에서 스캔하면 같은
      // 이름 N회 호출 시 마지막 항목만 덮어써져 앞 항목들이 running 고착).
      let filled = false;
      for (let i = 0; i < calls.length; i++) {
        if (calls[i].tool === event.tool && calls[i].result === undefined) {
          calls[i] = { ...calls[i], result: event.result, status: 'done' as const };
          filled = true;
          break;
        }
      }
      if (!filled) {
        calls.push({ tool: event.tool, result: event.result, status: 'done' as const });
      }
      return { ...msg, toolCalls: calls };
    }
    case 'chart': {
      // 차트로 추출된 원문 JSON 블록(strip)을 표시 텍스트에서 제거 — 차트가
      // 별도 렌더되므로 본문에 raw JSON 이 중복 노출되지 않게.
      const strip = (event as { strip?: string }).strip;
      const cleaned =
        strip && msg.content.includes(strip)
          ? msg.content.split(strip).join('').replace(/\n{3,}/g, '\n\n').trimEnd()
          : msg.content;
      return {
        ...msg,
        content: cleaned,
        charts: [...(msg.charts || []), event.spec as ChartSpec],
      };
    }
    case 'plan': {
      // deep 모드 분석 계획(§57) — 본문에서 raw 펜스 제거하고 구조화 카드로.
      const strip = (event as { strip?: string }).strip;
      const cleaned =
        strip && msg.content.includes(strip)
          ? msg.content.split(strip).join('').replace(/\n{3,}/g, '\n\n').trimEnd()
          : msg.content;
      return { ...msg, content: cleaned, plan: event.plan };
    }
    case 'report': {
      const r = event as { s3_uri: string; file_name: string; format: string; summary: string; page_count?: number | null };
      return {
        ...msg,
        reports: [
          ...(msg.reports || []),
          { s3_uri: r.s3_uri, file_name: r.file_name, format: r.format, summary: r.summary, page_count: r.page_count },
        ],
      };
    }
    case 'validator':
      return { ...msg, validator: event.result as ValidatorResult };
    case 'verification':
      // L3 실행기반 후보선택 검증(§58, deep 모드만) — "검증됨" 카드 누적.
      return {
        ...msg,
        verifications: [...(msg.verifications || []), event.result as VerificationResult],
      };
    case 'audit':
      // L5 답변 감사(§60, deep 모드만) — 최종 산문 수치 cite 무결성. validator 와
      // 별개 레이어(비파괴 advisory 카드). RETRY/NEEDS_REVIEW 일 때만 도착.
      return { ...msg, audit: event.result as AuditResult };
    case 'text':
      return { ...msg, content: msg.content + (event.chunk || '') };
    case 'done': {
      // [SUGGESTIONS]q1|q2|q3[/SUGGESTIONS] 추출(§55) — 본문에서 제거하고 칩으로.
      let content = msg.content;
      let suggestions = msg.suggestions;
      const m = content.match(/\[SUGGESTIONS\]([\s\S]*?)\[\/SUGGESTIONS\]/);
      if (m) {
        suggestions = m[1].split('|').map((s) => s.trim()).filter(Boolean).slice(0, 3);
        content = content.replace(m[0], '').trimEnd();
      }
      return {
        ...msg,
        content,
        suggestions,
        pending: false,
        costUsd: event.costUsd,
        durationMs: event.durationMs,
        // tool_result 가 없는 도구(render_chart 는 chart 이벤트로만 결과 발행)는
        // running 으로 남아 "실행 중..." 이 영구 표시됨 — done 에서 일괄 완료 처리.
        toolCalls: (msg.toolCalls || []).map((c) =>
          c.status === 'running' ? { ...c, status: 'done' as const } : c
        ),
      };
    }
    case 'error':
      return {
        ...msg,
        pending: false,
        content: msg.content + `\n\n[오류] ${event.error}`,
        // 에러 종료 시에도 running 도구를 정리(스피너 영구 표시 방지).
        toolCalls: (msg.toolCalls || []).map((c) =>
          c.status === 'running' ? { ...c, status: 'failed' as const } : c
        ),
      };
    default:
      return msg;
  }
}
