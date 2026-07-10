// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import type { AnalyticsFilterForm } from '@/types/api';
import type { PeriodType, GroupByType } from '@/types/enums';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';
import { AnalyticsFilter } from '@/components/analytics/AnalyticsFilter';
import { ROIMetricsCards } from '@/components/analytics/ROIMetricsCards';
import { CostTrendChart } from '@/components/analytics/CostTrendChart';
import { BreakdownChart } from '@/components/analytics/BreakdownChart';
// ProductivityCards 제거 — git/IDE 연동 파이프라인 미구축으로 productivity_events·
// git_events 가 항상 0행이라 코드라인/커밋/PR/개발자 카드가 무의미. 연동 구현
// 후 복원. DEVLOG §21 "개발 필요" 참조. (ingest 엔드포인트는 admin-api 에 보존.)
import { ExportButton } from '@/components/analytics/ExportButton';
import { RefreshButton } from '@/components/analytics/RefreshButton';
import { fetchAvailablePeriods } from '@/lib/actions/dashboard';
import { isMonth } from '@/lib/utils/period';

interface AnalyticsPageProps {
  searchParams: {
    period?: string;
    start_date?: string;
    end_date?: string;
    group_by?: string;
    scope?: string;
  };
}

/**
 * period 는 이제 'YYYY-MM'(월 선택기) 또는 'custom'(직접 날짜)만 의미를 가진다.
 * 레거시 7d/30d/90d 가 들어와도 월 선택기가 effectiveMonth 로 덮어쓰므로 무해.
 * filter.period 에는 custom 이면 'custom', 아니면 실제 월 문자열을 그대로 실어
 * resolveMonth 가 존중하게 한다.
 */
function parseFilter(
  searchParams: AnalyticsPageProps['searchParams'],
  effectiveMonth: string,
): AnalyticsFilterForm {
  const validGroupBys: GroupByType[] = ['model', 'team', 'user'];
  const isCustom = searchParams.period === 'custom';

  const group_by = validGroupBys.includes(searchParams.group_by as GroupByType)
    ? (searchParams.group_by as GroupByType)
    : 'model';

  return {
    // custom 이면 'custom', 아니면 적용 월(YYYY-MM)을 period 에 직접 — resolveMonth 가 honor.
    period: (isCustom ? 'custom' : effectiveMonth) as PeriodType,
    group_by,
    start_date: searchParams.start_date ?? null,
    end_date: searchParams.end_date ?? null,
    scope: searchParams.scope ?? null,
  };
}

const GROUP_BY_LABEL: Record<GroupByType, string> = {
  model: '모델별',
  team: '팀별',
  user: '사용자별',
};

/**
 * 퀵챗 화면 컨텍스트 등록 — "지금 보는 분석 화면(적용 월/그룹)".
 *
 * analytics 는 표시 데이터를 자식 컴포넌트(ROIMetricsCards 등)가 각자 fetch 하므로
 * (모니터링과 달리 페이지 레벨 데이터가 없음), 컨텍스트 등록용으로 동일 집계 요약을
 * 독립적으로 한 번 더 조회한다. 자체 Suspense 경계 → 본문 렌더를 막지 않음.
 * cost_summary 는 순수 집계(요청/토큰/비용/활성자 수)라 PII 없음 — 헤드라인 수치만 동봉.
 */
async function ContextSection({
  filter,
  latestMonth,
  periodLabel,
}: {
  filter: AnalyticsFilterForm;
  latestMonth: string;
  periodLabel: string;
}) {
  interface CostSummaryResponse {
    cost_summary?: {
      total_requests: number;
      total_tokens: number;
      total_cost_usd: number;
      active_users: number;
      avg_cost_per_user_usd: number;
    };
  }
  // 표시 컴포넌트(ROIMetricsCards 등)와 **동일** buildAnalyticsQuery → 바이트 동일
  // 요청 → Next.js 요청 메모이제이션으로 1회 호출로 dedup(중복 네트워크 방지).
  const data = await adminAPI
    .get<CostSummaryResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  const s = data?.cost_summary;
  return (
    <RegisterScreenContext
      page="사용량 분석 (ROI)"
      period={periodLabel}
      data={{
        그룹기준: GROUP_BY_LABEL[filter.group_by],
        ...(s
          ? {
              총요청수: s.total_requests,
              총토큰수: s.total_tokens,
              총비용USD: Number(s.total_cost_usd ?? 0),
              활성사용자수: s.active_users,
              사용자당평균비용USD: Number(s.avg_cost_per_user_usd ?? 0),
            }
          : {}),
      }}
    />
  );
}

export default async function AnalyticsPage({ searchParams }: AnalyticsPageProps) {
  // 월 데이터 소스 (대시보드와 동일: /admin/dashboard/periods).
  const { periods, latest } = await fetchAvailablePeriods();

  // 적용 월 결정: ?period 가 데이터 있는 월이면 존중, 아니면 latest.
  const requested = searchParams.period;
  const effectiveMonth = isMonth(requested) && periods.includes(requested) ? requested : latest;

  const filter = parseFilter(searchParams, effectiveMonth);
  // 차트 key: custom 이면 날짜, 아니면 월 — 변경 시 remount + 재요청.
  const sectionKey = filter.period === 'custom' ? `custom-${filter.start_date ?? ''}` : effectiveMonth;
  // 컨텍스트용 사람이 읽는 기간 라벨.
  const periodLabel =
    filter.period === 'custom'
      ? `${filter.start_date ?? '?'} ~ ${filter.end_date ?? '?'}`
      : `${effectiveMonth} 월간`;

  return (
    <div className="space-y-6">
      {/* 퀵챗 화면 컨텍스트 등록(렌더 null) — 적용 월/그룹/집계 헤드라인. PII 없음. */}
      <Suspense key={`ctx-${sectionKey}-${filter.group_by}`} fallback={null}>
        <ContextSection filter={filter} latestMonth={effectiveMonth} periodLabel={periodLabel} />
      </Suspense>

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">분석 (ROI 대시보드)</h1>
        <div className="flex gap-2">
          <RefreshButton />
          <ExportButton filter={filter} latestMonth={effectiveMonth} />
        </div>
      </div>

      {/* 필터 — 월 선택기 + custom + group_by */}
      <AnalyticsFilter defaultValue={filter} periods={periods} currentMonth={effectiveMonth} />

      {/* 각 섹션은 개별 Suspense. key 에 적용 월/날짜 포함 — 변경 시 재요청 */}
      <Suspense key={`roi-${sectionKey}`} fallback={<SkeletonCard count={4} />}>
        <ROIMetricsCards filter={filter} latestMonth={effectiveMonth} />
      </Suspense>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Suspense key={`trend-${sectionKey}`} fallback={<SkeletonCard count={1} />}>
          <CostTrendChart filter={filter} latestMonth={effectiveMonth} />
        </Suspense>
        <Suspense key={`breakdown-${sectionKey}`} fallback={<SkeletonCard count={1} />}>
          <BreakdownChart filter={filter} latestMonth={effectiveMonth} />
        </Suspense>
      </div>
    </div>
  );
}
