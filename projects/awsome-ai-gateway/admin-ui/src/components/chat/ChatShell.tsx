// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 통합 셸 — 본문과 퀵챗 패널 배치. 화면 폭에 따라 두 모드:
 *
 *   넓은 화면(≥1024px): flex 형제 분할뷰(overlay 아님)
 *     [ 본문영역(flex-1) | ResizeHandle | ChatPanel(width px) ]
 *     채팅이 열리면 본문(flex-1)이 패널 폭만큼 **자동으로 좁아진다** — margin 트릭/
 *     동기화 계산 없이 폭 상태(width) 하나만 관리.
 *
 *   좁은 화면(<1024px): overlay 드로어(본문 위 + 백드롭)
 *     분할뷰는 본문이 0 으로 짓눌리므로 master-detail 단일 페인으로 폴백.
 *
 * 닫히면 본문이 전체를 차지하고 우하단 토글 FAB 만 남는다.
 * layout.tsx 의 <main> 자리를 이 셸이 감싼다(children = 페이지).
 */

import { useEffect, useRef, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { MessageSquare, X, Sparkles } from 'lucide-react';
import { ChatProvider, useChatPanel, CHAT_MIN_WIDTH, CHAT_MAX_WIDTH } from './ChatProvider';
import { ChatLayout } from './ChatLayout';

// 분할뷰가 편안한 최소 폭. Sidebar(256)+본문(≥360)+핸들+패널(360) ≈ 980 →
// 이보다 좁으면 본문이 0 으로 짓눌리므로 overlay 드로어로 폴백(master-detail 축약).
const SPLIT_MIN_VIEWPORT = 1024;

/** 뷰포트가 분할뷰에 충분히 넓은지. 좁으면 overlay 드로어로 전환. */
function useIsWideViewport(): boolean {
  // SSR/첫 페인트는 wide 가정(데스크톱 기본). 마운트 후 실제 폭으로 정정.
  const [wide, setWide] = useState(true);
  useEffect(() => {
    const mql = window.matchMedia(`(min-width: ${SPLIT_MIN_VIEWPORT}px)`);
    const sync = () => setWide(mql.matches);
    sync();
    mql.addEventListener('change', sync);
    return () => mql.removeEventListener('change', sync);
  }, []);
  return wide;
}

/** 드래그로 패널 폭 조절. 본문/패널 경계의 세로 핸들. */
function ResizeHandle() {
  const { width, setWidth } = useChatPanel();
  const dragging = useRef(false);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (!dragging.current) return;
      // 패널은 오른쪽 → 폭 = 창 너비 - 마우스 X.
      setWidth(window.innerWidth - e.clientX);
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [setWidth]);

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="채팅 패널 폭 조절"
      onPointerDown={() => {
        dragging.current = true;
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
      }}
      title={`폭 ${Math.round(width)}px (드래그로 조절)`}
      className="w-1 flex-shrink-0 cursor-col-resize bg-border hover:bg-primary/50 active:bg-primary transition-colors"
    />
  );
}

/** 우하단 FAB — 패널 **열기** 전용. 열려 있을 땐 렌더 안 함(닫기는 PanelHeader 의
 *  X 가 담당). FAB 가 패널 위에 떠 있으면 입력창 전송 버튼과 위치가 겹치므로
 *  (둘 다 우하단), 열렸을 땐 숨겨 충돌을 없앤다 — 표준 FAB-드로어 패턴. */
function ChatToggleButton() {
  const { open, toggle } = useChatPanel();
  if (open) return null;
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label="Quick Chat 열기"
      className="fixed bottom-6 right-6 z-30 flex h-14 w-14 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-transform hover:scale-105 hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <MessageSquare size={22} />
    </button>
  );
}

/** 채팅 패널 헤더 — 전체화면 이동 + 닫기. */
function PanelHeader() {
  const { setOpen } = useChatPanel();
  const router = useRouter();
  return (
    <div className="flex items-center justify-between gap-1 border-b border-border px-2 py-2">
      {/* 퀵챗 → BI Insight(심층분석, 별도 세션)로 이동. '전체화면' 이 아니라 다른
          기능으로 넘어가므로 라벨/아이콘으로 명시(Maximize 아이콘은 오해 소지). */}
      <button
        type="button"
        onClick={() => {
          setOpen(false);
          router.push('/chat');
        }}
        aria-label="BI Insight 심층 분석으로 열기"
        title="BI Insight(심층 분석)로 이동 — 별도 세션"
        className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
      >
        <Sparkles size={14} />
        BI Insight
      </button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        aria-label="닫기"
        className="flex h-8 w-8 items-center justify-center rounded-md hover:bg-accent"
      >
        <X size={18} />
      </button>
    </div>
  );
}

function ShellInner({ children, enabled }: { children: React.ReactNode; enabled: boolean }) {
  const { open, setOpen, width } = useChatPanel();
  const isWide = useIsWideViewport();
  // /chat 풀페이지에선 드로어/ FAB 를 띄우지 않는다 — 그 페이지가 이미 채팅이라
  // 중복이고, 우하단 FAB 가 입력창 전송 버튼과 겹친다(같은 코너). enabled 와 무관히 OFF.
  const pathname = usePathname();
  const onChatPage = pathname === '/chat' || pathname?.startsWith('/chat/');
  const chatEnabled = enabled && !onChatPage;
  // 패널을 처음 연 뒤에만 ChatLayout 마운트(세션 생성 지연). 닫아도 유지(대화 보존).
  const mountedRef = useRef(false);
  if (open) mountedRef.current = true;

  const panelOpen = chatEnabled && open;
  // 넓은 화면: flex 형제 분할뷰(본문 자동 축소). 좁은 화면: 본문 위 overlay 드로어(modal).
  const splitView = panelOpen && isWide;
  const overlay = panelOpen && !isWide;

  // overlay(modal) 모드에서 Esc 로 닫기 — 백드롭이 암시하는 모달 시맨틱과 일치.
  useEffect(() => {
    if (!overlay) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [overlay, setOpen]);

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* 본문 — 분할뷰일 때만 flex-1 이 패널 폭만큼 좁아짐. overlay 모드면 전체 폭 유지 */}
      <div className="flex flex-1 flex-col overflow-hidden min-w-0">{children}</div>

      {/* 좁은 화면 overlay 백드롭(탭으로 닫기). 분할뷰/닫힘이면 false(슬롯만 유지). */}
      {overlay && (
        <button
          type="button"
          aria-label="채팅 닫기"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-40 bg-black/40"
        />
      )}

      {/* 분할뷰 리사이즈 핸들 — 넓은 화면에서만. overlay/닫힘이면 false(슬롯만 유지). */}
      {splitView && <ResizeHandle />}

      {/* 채팅 패널 — **단일 <aside> 인스턴스, 처음 연 뒤 항상 DOM 유지**(닫으면
          display:none 으로 숨김만). ChatLayout 은 sessionId/messages 를 자기 useState
          로 들고 SSE 스트림을 열어두므로, 언마운트되면 대화가 사라진다. 따라서:
            · 닫아도 유지: panelOpen=false 면 className='hidden'(언마운트 아님).
            · 폭 경계(1024px) 넘어도 유지: 모드는 className/role/style 만 교체, JSX
              위치 고정 → React 가 같은 노드로 재조정해 ChatLayout 재마운트 없음.
          분할뷰는 본문이 0 으로 짓눌리므로 좁은 화면은 overlay(modal) 단일 페인 폴백. */}
      {mountedRef.current && (
        <aside
          role={overlay ? 'dialog' : 'complementary'}
          aria-modal={overlay ? true : undefined}
          aria-label="Quick Chat"
          style={splitView ? { width: Math.min(CHAT_MAX_WIDTH, Math.max(CHAT_MIN_WIDTH, width)) } : undefined}
          className={
            !panelOpen
              ? 'hidden'
              : overlay
                ? 'fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-border bg-background shadow-xl'
                : 'flex flex-shrink-0 flex-col border-l border-border bg-background'
          }
        >
          <PanelHeader />
          <div className="flex-1 overflow-hidden">
            <ChatLayout variant="drawer" />
          </div>
        </aside>
      )}

      {chatEnabled && <ChatToggleButton />}
    </div>
  );
}

/**
 * 셸 진입점. enabled=false(비-ADMIN)면 채팅 UI 없이 본문만 — 단 Provider 는
 * 항상 감싸 hook 안전성 유지(페이지의 useRegisterScreenContext 가 throw 안 하게).
 */
export function ChatShell({
  children,
  enabled,
}: {
  children: React.ReactNode;
  enabled: boolean;
}) {
  return (
    <ChatProvider>
      <ShellInner enabled={enabled}>{children}</ShellInner>
    </ChatProvider>
  );
}
