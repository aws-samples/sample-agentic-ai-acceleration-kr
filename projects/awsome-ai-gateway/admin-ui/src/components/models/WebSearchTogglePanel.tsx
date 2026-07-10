'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState, useTransition } from 'react';
import { setClientWebSearchAction, type RoutingProfileItem } from '@/lib/actions/routing';
import { labelFor } from '@/lib/utils/modelLabel';

// 앱(client) 순서 고정 — client_identifier 토큰과 동일.
const CLIENT_ORDER = ['claude-code', 'cowork', 'codex'] as const;

interface Props {
  initial: RoutingProfileItem[];
}

/**
 * 앱(client)별 웹서치 허용 토글. routing_profiles.web_search_enabled 를 제어.
 * ON 이면 게이트웨이가 해당 client 요청에 web_search 툴을 주입(서버사이드 검색).
 * 변경 시 gateway-proxy Redis 캐시(routing_profile:{client})가 무효화되어 즉시 반영.
 */
export function WebSearchTogglePanel({ initial }: Props) {
  // client → enabled 맵으로 정규화 (initial 에 없는 client 는 미표시)
  const initialMap = new Map(initial.map((p) => [p.client, p.web_search_enabled]));
  const [state, setState] = useState<Record<string, boolean>>(
    Object.fromEntries(CLIENT_ORDER.filter((c) => initialMap.has(c)).map((c) => [c, !!initialMap.get(c)])),
  );
  const [pending, startTransition] = useTransition();
  const [busyClient, setBusyClient] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const clients = CLIENT_ORDER.filter((c) => c in state);

  function toggle(client: string) {
    const next = !state[client];
    setError(null);
    setBusyClient(client);
    // optimistic
    setState((s) => ({ ...s, [client]: next }));
    startTransition(async () => {
      const res = await setClientWebSearchAction(client, next);
      if (!res.success) {
        // rollback on failure
        setState((s) => ({ ...s, [client]: !next }));
        setError(res.error);
      }
      setBusyClient(null);
    });
  }

  if (clients.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        라우팅 프로필이 없습니다. (마이그레이션 0021 로 claude-code/cowork/codex 시드 필요)
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        앱별 서버사이드 웹서치 허용. ON 이면 해당 앱(Claude Code / Cowork / Codex) 사용자가
        질문할 때 게이트웨이가 자동으로 웹 검색을 수행해 최신 정보로 답합니다(클라 설정 불필요).
      </p>
      <div className="flex flex-wrap gap-3">
        {clients.map((client) => {
          const on = state[client];
          const busy = pending && busyClient === client;
          return (
            <button
              key={client}
              type="button"
              disabled={busy}
              onClick={() => toggle(client)}
              aria-pressed={on}
              className={[
                'pressable rounded-apple-sm px-4 py-2 text-sm font-medium transition-[background,color,box-shadow] duration-150 flex items-center gap-2',
                on
                  ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
                  : 'text-muted-foreground interactive',
                busy ? 'opacity-60 cursor-wait' : '',
              ].join(' ')}
            >
              <span
                className={[
                  'inline-block h-2 w-2 rounded-full',
                  on ? 'bg-primary' : 'bg-muted-foreground/40',
                ].join(' ')}
                aria-hidden
              />
              {labelFor(client)}
              <span className="text-xs opacity-70">{on ? '웹서치 ON' : '웹서치 OFF'}</span>
            </button>
          );
        })}
      </div>
      {error && <div className="text-sm text-destructive">저장 실패: {error}</div>}
    </div>
  );
}
