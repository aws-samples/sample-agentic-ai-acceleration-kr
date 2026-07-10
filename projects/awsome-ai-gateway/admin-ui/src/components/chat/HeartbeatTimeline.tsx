// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useState } from 'react';
import { Search, Database, LineChart, ShieldCheck, BarChart3, FileText, Loader2, Check } from 'lucide-react';
import type { HeartbeatPhase } from './types';

/**
 * 공백 없는 스트리밍 — heartbeat 진행 타임라인("화려한" 생존신호).
 *
 * 5-agent 파이프라인은 sub-agent 한 번이 20~60초 blocking 이라, 그 침묵 구간을
 * agent 가 5초마다 흘리는 heartbeat 프레임으로 메운다. reasoning("사고 과정"
 * 텍스트)과는 다른 레인 — 이쪽은 **구조적 단계 타임라인**:
 *   - 단계가 하나씩 채워지는 세로 체크리스트(완료=체크, 진행=spinner+shimmer)
 *   - 진행 중 단계에 흐르는 shimmer 그라데이션
 *   - 서버 프레임(5s) 사이를 클라가 보간하는 **라이브 경과 카운터**(1s)
 *
 * 본문(content) 첫 토큰이 도착하면 부모가 이 컴포넌트를 더 이상 렌더하지 않으므로
 * (live=false 경로), interval 은 pending && heartbeats 일 때만 돈다(타이머 누수 방지).
 */

const PHASE_META: Record<string, { icon: typeof Search; tint: string }> = {
  think: { icon: Search, tint: 'text-sky-500' },
  sql: { icon: Database, tint: 'text-violet-500' },
  analyze: { icon: LineChart, tint: 'text-amber-500' },
  validate: { icon: ShieldCheck, tint: 'text-emerald-500' },
  viz: { icon: BarChart3, tint: 'text-pink-500' },
  report: { icon: FileText, tint: 'text-orange-500' },
  work: { icon: Loader2, tint: 'text-muted-foreground' },
};

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}초`;
  return `${Math.floor(s / 60)}분 ${s % 60}초`;
}

export function HeartbeatTimeline({
  phases,
  heartbeatAt,
}: {
  phases: HeartbeatPhase[];
  heartbeatAt?: number;
}) {
  // 라이브 경과 보간: 마지막 heartbeat 의 elapsedMs(서버 기준) + 그 이후 클라 경과.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (!phases.length) return null;
  const last = phases[phases.length - 1];
  const base = last.elapsedMs;
  const sinceLast = heartbeatAt ? Math.max(0, now - heartbeatAt) : 0;
  const liveElapsed = base + sinceLast;

  return (
    <div className="mb-2 rounded-lg border border-border/60 bg-gradient-to-br from-muted/30 to-muted/10 px-3 py-2.5">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
        </span>
        <span>처리 중</span>
        <span className="ml-auto tabular-nums text-muted-foreground/70">{fmtElapsed(liveElapsed)}</span>
      </div>
      <ol className="flex flex-col gap-1.5">
        {phases.map((p, i) => {
          const isActive = i === phases.length - 1;
          const meta = PHASE_META[p.phase] || PHASE_META.work;
          const Icon = meta.icon;
          return (
            <li key={`${p.phase}-${i}`} className="flex items-center gap-2 text-xs">
              <span
                className={[
                  'flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full',
                  isActive ? `bg-background ${meta.tint}` : 'bg-background text-emerald-500',
                ].join(' ')}
              >
                {isActive ? <Icon size={12} className="animate-spin-slow" /> : <Check size={12} />}
              </span>
              <span
                className={[
                  'relative overflow-hidden rounded px-1.5 py-0.5',
                  isActive ? 'text-foreground' : 'text-muted-foreground/70',
                ].join(' ')}
              >
                {p.label}
                {p.count > 1 && <span className="ml-1 text-muted-foreground/50">×{p.count}</span>}
                {isActive && (
                  // shimmer — 진행 중 단계에 흐르는 그라데이션 스윕.
                  <span className="pointer-events-none absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-foreground/10 to-transparent" />
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
