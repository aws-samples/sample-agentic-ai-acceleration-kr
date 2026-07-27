// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/metrics/summary — AgentCore Runtime 메트릭 요약 카드.
 *
 * 네임스페이스는 `AWS/Bedrock-AgentCore`(AgentCore 벤디드 메트릭). 차원 조합은 리소스/버전에
 * 따라 달라지므로 **metric math `SEARCH()`** 로 네임스페이스 전체를 합산한다. 차원 이름을
 * 하드코딩하지 않으므로 런타임이 추가돼도 쿼리 수정이 필요 없고, 데이터가 없으면 값이 비어
 * `null` 로 graceful 하게 내려간다(화면은 "—" 표시).
 *
 * 기간은 쿼리 `hours`(기본 24, 최대 168)로 조정한다.
 */

import { GetMetricDataCommand, type MetricDataQuery } from '@aws-sdk/client-cloudwatch';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { cloudWatchClient } from '@/lib/aws-clients';
import type { MetricSummaryItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

const NAMESPACE = 'AWS/Bedrock-AgentCore';

/** 카드 정의 — SEARCH 로 모은 시계열을 SUM/AVG 로 단일 스칼라화한다. */
const CARDS: Array<{
  key: string;
  label: string;
  metric: string;
  stat: 'Sum' | 'Average';
  /** 여러 시계열을 하나로 접는 방식. */
  fold: 'SUM' | 'AVG';
  unit?: string;
}> = [
  { key: 'invocations', label: '호출 수', metric: 'Invocations', stat: 'Sum', fold: 'SUM' },
  { key: 'sessions', label: '세션 수', metric: 'SessionCount', stat: 'Sum', fold: 'SUM' },
  {
    key: 'latency_ms',
    label: '평균 지연',
    metric: 'Latency',
    stat: 'Average',
    fold: 'AVG',
    unit: 'ms',
  },
  { key: 'throttles', label: '스로틀', metric: 'Throttles', stat: 'Sum', fold: 'SUM' },
  { key: 'user_errors', label: '사용자 오류', metric: 'UserErrors', stat: 'Sum', fold: 'SUM' },
  { key: 'system_errors', label: '시스템 오류', metric: 'SystemErrors', stat: 'Sum', fold: 'SUM' },
];

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    const hoursParam = Number(new URL(request.url).searchParams.get('hours'));
    const hours = Number.isFinite(hoursParam) && hoursParam > 0 ? Math.min(hoursParam, 168) : 24;
    const endTime = new Date();
    const startTime = new Date(endTime.getTime() - hours * 3600 * 1000);
    // 기간 전체를 한 데이터포인트로 접기 위해 period 를 조회 창 길이로 맞춘다.
    const period = hours * 3600;

    const queries: MetricDataQuery[] = CARDS.map((card, index) => ({
      Id: `m${index}`,
      Expression:
        `${card.fold}(SEARCH('{${NAMESPACE}} MetricName="${card.metric}"', ` +
        `'${card.stat}', ${period}))`,
      Period: period,
      ReturnData: true,
    }));

    let values: Record<string, number | null> = {};
    let note: string | undefined;
    try {
      const out = await cloudWatchClient().send(
        new GetMetricDataCommand({
          StartTime: startTime,
          EndTime: endTime,
          MetricDataQueries: queries,
        })
      );
      for (const result of out.MetricDataResults ?? []) {
        if (!result.Id) continue;
        const points = result.Values ?? [];
        values[result.Id] = points.length ? points[0] : null;
      }
    } catch (error) {
      // 메트릭이 아직 발행되지 않았거나 권한/쿼리 문제여도 대시보드는 떠야 한다.
      console.warn('[admin-metrics] GetMetricData 실패 — 빈 요약으로 응답:', error);
      values = {};
      note = '메트릭을 조회할 수 없습니다 (데이터 미발행 또는 권한 확인 필요)';
    }

    const items: MetricSummaryItem[] = CARDS.map((card, index) => ({
      key: card.key,
      label: card.label,
      value: values[`m${index}`] ?? null,
      unit: card.unit,
    }));

    return Response.json(
      {
        status: 'ok',
        namespace: NAMESPACE,
        window_hours: hours,
        items,
        ...(note ? { note } : {}),
      },
      { status: 200 }
    );
  });
}
