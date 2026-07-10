'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useEffect, useTransition } from 'react';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitScope } from '@/types/enums';
import { setRateLimitAction } from '@/lib/actions/rate-limits';
import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface RateLimitConfigPanelProps {
  node: RateLimitTreeNode | null;
}

const SCOPE_LABEL: Record<string, string> = {
  GLOBAL: '전역',
  TEAM: '팀',
  USER: '사용자',
};

function parsePosInt(val: string): number | null {
  const n = parseInt(val, 10);
  return isNaN(n) || n <= 0 ? null : n;
}

function parsePosFloat(val: string): number | null {
  const n = parseFloat(val);
  return isNaN(n) || n <= 0 ? null : n;
}

export function RateLimitConfigPanel({ node }: RateLimitConfigPanelProps) {
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const [rpm, setRpm] = useState('');
  const [tpm, setTpm] = useState('');
  const [cpm, setCpm] = useState('');
  const [cph, setCph] = useState('');
  const [usage, setUsage] = useState<RateLimitUsage | null>(null);

  // node가 변경될 때 폼 값 초기화
  useEffect(() => {
    if (node?.config) {
      setRpm(node.config.rpm != null ? String(node.config.rpm) : '');
      setTpm(node.config.tpm != null ? String(node.config.tpm) : '');
      setCpm(node.config.cpm != null ? String(node.config.cpm) : '');
      setCph(node.config.cph != null ? String(node.config.cph) : '');
    } else {
      setRpm('');
      setTpm('');
      setCpm('');
      setCph('');
    }
    setError(null);
  }, [node]);

  // 실시간 RPM 사용량(§60.9) — node 선택 시 + 10초마다 폴링(gateway-proxy Redis 카운터).
  useEffect(() => {
    if (!node || node.scope === RateLimitScope.GLOBAL) {
      setUsage(null);
      return;
    }
    let alive = true;
    const load = () =>
      fetchRateLimitUsage(node.scope, node.id)
        .then((u) => { if (alive) setUsage(u); })
        .catch(() => { if (alive) setUsage(null); });
    load();
    const t = setInterval(load, 10_000);
    return () => { alive = false; clearInterval(t); };
  }, [node]);

  if (!node) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        트리에서 노드를 선택하세요
      </div>
    );
  }

  const isUserOrTeam =
    node.scope === RateLimitScope.USER || node.scope === RateLimitScope.TEAM;

  const inheritedRpm = node.inherited_from && node.config?.rpm != null
    ? String(node.config.rpm)
    : undefined;
  const inheritedTpm = node.inherited_from && node.config?.tpm != null
    ? String(node.config.tpm)
    : undefined;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    startTransition(async () => {
      const result = await setRateLimitAction({
        target_id: node.id,
        scope: node.scope,
        rpm: parsePosInt(rpm),
        tpm: parsePosInt(tpm),
        cpm: isUserOrTeam ? parsePosFloat(cpm) : undefined,
        cph: isUserOrTeam ? parsePosFloat(cph) : undefined,
      });

      if (result.success) {
        toast({
          type: 'success',
          message: `${node.label}의 Rate Limit이 저장되었습니다.`,
          auto_dismiss_ms: 3000,
        });
      } else {
        setError(result.error);
      }
    });
  };

  return (
    <div>
      {/* 헤더 */}
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold">{node.label}</h2>
        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold">
          {SCOPE_LABEL[node.scope] ?? node.scope}
        </span>
      </div>

      {/* 상속 정보 */}
      {node.inherited_from && (
        <div className="mb-4 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
          상위 설정 상속 중: <span className="font-medium text-foreground">{node.inherited_from}</span>
        </div>
      )}

      {/* 실시간 사용량(§60.9) — gateway-proxy Redis sliding-window 카운터. 10초 폴링. */}
      {usage?.available && (
        <div className="mb-4 rounded-md border border-border bg-card/50 px-3 py-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              실시간 RPM 사용량 (최근 {usage.window_sec}초)
            </span>
            <span className="num text-sm font-semibold text-foreground">
              {usage.rpm_used_total}
              {node.config?.rpm != null && (
                <span className="text-muted-foreground font-normal"> / {node.config.rpm}</span>
              )}
            </span>
          </div>
          {/* 한도 대비 게이지(설정 있을 때만) */}
          {node.config?.rpm != null && node.config.rpm > 0 && (
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${Math.min(100, (usage.rpm_used_total / node.config.rpm) * 100)}%` }}
              />
            </div>
          )}
          {usage.by_model.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-[11px] text-muted-foreground">
              {usage.by_model.slice(0, 5).map((m) => (
                <li key={m.model_alias} className="flex justify-between">
                  <span className="font-mono">{m.model_alias}</span>
                  <span className="num">{m.rpm_used}</span>
                </li>
              ))}
            </ul>
          )}
          {usage.rpm_used_total === 0 && (
            <p className="mt-1 text-[11px] text-muted-foreground">최근 {usage.window_sec}초간 요청 없음</p>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* RPM */}
        <div className="space-y-1">
          <label className="text-sm font-medium">RPM (Requests Per Minute)</label>
          <input
            type="number"
            min={1}
            value={rpm}
            onChange={(e) => setRpm(e.target.value)}
            placeholder={inheritedRpm ?? '제한없음'}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">비워두면 제한 없음</p>
        </div>

        {/* TPM */}
        <div className="space-y-1">
          <label className="text-sm font-medium">TPM (Tokens Per Minute)</label>
          <input
            type="number"
            min={1}
            value={tpm}
            onChange={(e) => setTpm(e.target.value)}
            placeholder={inheritedTpm ?? '제한없음'}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">비워두면 제한 없음</p>
        </div>

        {/* CPM — USER/TEAM only */}
        <div className="space-y-1">
          <label
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPM (Cost Per Minute, USD)
          </label>
          <input
            type="number"
            min={0}
            step="0.0001"
            value={cpm}
            onChange={(e) => setCpm(e.target.value)}
            placeholder="제한없음"
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">USER/TEAM 범위에서만 사용 가능</p>
          )}
        </div>

        {/* CPH — USER/TEAM only */}
        <div className="space-y-1">
          <label
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPH (Cost Per Hour, USD)
          </label>
          <input
            type="number"
            min={0}
            step="0.0001"
            value={cph}
            onChange={(e) => setCph(e.target.value)}
            placeholder="제한없음"
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">USER/TEAM 범위에서만 사용 가능</p>
          )}
        </div>

        <FormError error={error} />

        <div className="flex items-center justify-end pt-2">
          <SpinnerButton type="submit" isLoading={isPending}>
            저장
          </SpinnerButton>
        </div>
      </form>
    </div>
  );
}