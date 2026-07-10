// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 퀵챗 전역 상태 — 분할뷰(open/width) + 화면 컨텍스트(screenContext).
 *
 * 두 책임을 한 provider 에 둔다:
 *   1. 분할뷰: open(패널 열림), width(패널 px 폭). ChatToggleButton 이 토글,
 *      ResizeHandle 이 width 갱신, layout 셸이 본문/패널 배치에 소비.
 *   2. 화면 컨텍스트: 각 페이지가 "지금 보는 화면" 요약을 등록 → ChatLayout 이
 *      메시지 전송 시 동봉. 라우트 변경 시 자동 초기화(이전 화면 컨텍스트 잔존 방지).
 *
 * layout.tsx(server) 를 감싸되 children 은 그대로 통과(server component 유지).
 */

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useRef,
  type ReactNode,
} from 'react';
import { usePathname } from 'next/navigation';

/** 페이지가 등록하는 "지금 보는 화면" 요약. agent 에 동봉돼 컨텍스트 질의에 쓰임. */
export interface ScreenContext {
  /** 화면 이름 (예: "운영 모니터링", "사용량 분석"). */
  page: string;
  /** 적용된 기간/필터 요약 (예: "최근 1시간", "2026-06 월간"). */
  period?: string;
  /** 화면에 렌더된 핵심 데이터 요약(작게 — KPI/표 상위 N행). PII 주의. */
  data?: Record<string, unknown>;
}

interface ChatContextValue {
  // 분할뷰
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
  width: number;
  setWidth: (px: number) => void;
  // 화면 컨텍스트
  screenContext: ScreenContext | null;
  setScreenContext: (ctx: ScreenContext | null) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

// 패널 폭 제약 — 본문이 너무 좁아지지 않게 클램프.
export const CHAT_MIN_WIDTH = 360;
export const CHAT_MAX_WIDTH = 900;
export const CHAT_DEFAULT_WIDTH = 460;

export function ChatProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [width, setWidthRaw] = useState(CHAT_DEFAULT_WIDTH);
  const [screenContext, setScreenContext] = useState<ScreenContext | null>(null);
  const pathname = usePathname();
  const prevPath = useRef(pathname);

  // 라우트 변경 시 화면 컨텍스트 초기화 — "지금 보는 화면" 신선도 보장.
  // (새 페이지가 마운트되며 자기 컨텍스트를 다시 등록한다.)
  useEffect(() => {
    if (prevPath.current !== pathname) {
      prevPath.current = pathname;
      setScreenContext(null);
    }
  }, [pathname]);

  const setWidth = useCallback((px: number) => {
    setWidthRaw(Math.min(CHAT_MAX_WIDTH, Math.max(CHAT_MIN_WIDTH, px)));
  }, []);

  const toggle = useCallback(() => setOpen((o) => !o), []);

  return (
    <ChatContext.Provider
      value={{ open, setOpen, toggle, width, setWidth, screenContext, setScreenContext }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChatPanel(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error('useChatPanel must be used within ChatProvider');
  return ctx;
}

/** Provider 밖에서도 안전(null 반환). /chat 풀페이지처럼 컨텍스트 없는 곳용. */
export function useChatPanelOptional(): ChatContextValue | null {
  return useContext(ChatContext);
}

/**
 * 페이지가 자기 화면 컨텍스트를 등록하는 hook. 마운트 시 등록, 언마운트 시 정리.
 * server component 페이지는 직접 못 쓰므로 client 자식(또는 RegisterScreenContext)에서 호출.
 *
 * 예: useRegisterScreenContext({ page: "운영 모니터링", period: "최근 1시간", data });
 */
export function useRegisterScreenContext(ctx: ScreenContext | null) {
  const { setScreenContext } = useChatPanel();
  // ctx 를 직렬화해 의존성으로 — 내용이 바뀔 때만 재등록(매 렌더 방지).
  const key = ctx ? JSON.stringify(ctx) : null;
  useEffect(() => {
    setScreenContext(ctx);
    return () => setScreenContext(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}
