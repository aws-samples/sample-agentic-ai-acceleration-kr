'use client';
// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import { useState, useTransition } from 'react';

import { Badge } from '@/components/common/Badge';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { useToast } from '@/components/common/ToastProvider';
import { setBodyLoggingAction } from '@/lib/actions/settings';

interface Props {
  /** 서버에서 읽어 온 현재 저장 상태. */
  initialEnabled: boolean;
}

/**
 * 요청/응답 **본문** 로깅의 전역 on/off.
 *
 * 켜면 게이트웨이가 요청 JSON 전문과 응답(스트리밍이면 SSE 프레임 전문)을
 * Firehose → S3 로 보낸다. 즉 **사용자가 프롬프트에 넣은 것이 그대로 durable
 * 저장소로 나간다.** 현재 구현은 마스킹하지 않는다(알고 있는 격차, 향후 개선 대상).
 *
 * ⚠️ 그래서 **양방향 모두 확인 대화상자를 띄운다.** 원본 구현은 OFF 로 내릴 때만
 *    물었는데, 위험한 방향은 그 반대다 — 켜는 쪽이 사용자 프롬프트 수집을 시작하고,
 *    끄는 쪽은 수집을 멈추는(안전한) 방향이다. 위험한 액션이 클릭 한 번이고 안전한
 *    액션이 확인을 요구하면 확인 대화상자가 정확히 거꾸로 붙어 있는 것이다.
 *    끄는 쪽에도 확인을 남겨 둔 이유는 그것도 되돌릴 수 없는 결과(그 구간의 감사
 *    데이터가 영구히 없음)를 만들기 때문이다.
 *
 * 반영은 수 초 내(게이트웨이가 플래그를 짧게 캐시한다). 켜는 조작은 백엔드가
 * `audit.audit_logs` 에 불변 행으로 남긴다.
 */
export function BodyLoggingToggle({ initialEnabled }: Props) {
  const t = useTranslations('monitoring.bodyLogging');
  const { toast } = useToast();
  const [enabled, setEnabled] = useState(initialEnabled);
  const [isPending, startTransition] = useTransition();
  /** 확인을 기다리는 목표 상태. null = 대화상자 닫힘. */
  const [pendingNext, setPendingNext] = useState<boolean | null>(null);

  const apply = (next: boolean) => {
    setPendingNext(null);
    startTransition(async () => {
      const result = await setBodyLoggingAction(next);
      if (result.success) {
        setEnabled(result.data.enabled);
        toast({
          type: 'success',
          message: result.data.enabled ? t('turnedOn') : t('turnedOff'),
          auto_dismiss_ms: 3000,
        });
      } else {
        // ⚠️ 실패 시 스위치를 움직이지 않는다. 낙관적으로 먼저 뒤집으면 화면은 ON 인데
        //    실제로는 꺼진 채로 남아, 운영자가 수집되고 있다고 믿는 최악의 오답이 된다.
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  const handleClick = () => {
    if (isPending) return;
    setPendingNext(!enabled);
  };

  // 켜는 확인은 파괴적 스타일(빨강)로 띄운다 — 프라이버시에 영향을 주는 방향이다.
  const turningOn = pendingNext === true;

  return (
    <div className="glass rounded-apple p-4 flex items-center justify-between gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{t('label')}</span>
          <Badge tone={enabled ? 'amber' : 'neutral'}>{enabled ? 'ON' : 'OFF'}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">{t('description')}</p>
        {enabled && (
          // 켜져 있는 동안 상시 보이는 경고. 배지만으로는 "무엇이" 수집되는지 알 수 없다.
          <p className="text-xs text-destructive">{t('activeWarning')}</p>
        )}
      </div>

      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        aria-label={t('toggleLabel')}
        disabled={isPending}
        onClick={handleClick}
        className={[
          'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          enabled ? 'bg-primary' : 'bg-muted-foreground/40',
          isPending ? 'opacity-60 cursor-wait' : 'cursor-pointer',
        ].join(' ')}
      >
        <span
          className={[
            'inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform',
            enabled ? 'translate-x-5' : 'translate-x-0.5',
          ].join(' ')}
        />
      </button>

      <ConfirmDialog
        isOpen={pendingNext !== null}
        title={turningOn ? t('confirmOnTitle') : t('confirmOffTitle')}
        message={turningOn ? t('confirmOnMessage') : t('confirmOffMessage')}
        confirmLabel={turningOn ? t('confirmOnLabel') : t('confirmOffLabel')}
        isDestructive={turningOn}
        onConfirm={() => apply(pendingNext === true)}
        onClose={() => setPendingNext(null)}
      />
    </div>
  );
}
