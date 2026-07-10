// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';
import { resolveMonth } from '@/lib/utils/period';

/**
 * `/admin/analytics` 질의 파라미터 빌더 — **단일 출처**.
 *
 * analytics 페이지의 여러 서버 컴포넌트(ROIMetricsCards/CostTrendChart/BreakdownChart)
 * 와 퀵챗 컨텍스트 등록(ContextSection)이 모두 같은 엔드포인트를 같은 파라미터로
 * 호출한다. Next.js **요청 메모이제이션**은 동일 렌더 내 동일 URL+옵션 fetch 를
 * 1회로 dedup 하므로, 모든 호출처가 이 함수를 거쳐 **바이트 동일 요청**을 만들어야
 * 중복 네트워크 호출이 발생하지 않는다(파라미터가 갈리면 dedup 이 조용히 깨짐).
 */
export function buildAnalyticsQuery(
  filter: AnalyticsFilterForm,
  latestMonth?: string,
): Record<string, string | number | undefined> {
  return {
    period: resolveMonth(filter, latestMonth),
    group_by: filter.group_by,
    scope: filter.scope ?? 'all',
  };
}
