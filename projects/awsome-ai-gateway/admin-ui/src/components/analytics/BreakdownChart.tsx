// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import dynamic from 'next/dynamic';
import type { AnalyticsFilterForm } from '@/types/api';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';

interface BreakdownChartProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  by_model: { model: string; requests: number; cost_usd: number }[];
  by_team: { team: string; team_id: string; cost_usd: number; active_users: number }[];
  by_user: { user: string; email: string; cost_usd: number; requests: number }[];
}

// SSR 비활성화로 Chart.js window 참조 오류 방지
const BreakdownChartClient = dynamic(
  () =>
    import('./BreakdownChartClient').then((mod) => ({ default: mod.BreakdownChartClient })),
  { ssr: false }
);

export async function BreakdownChart({ filter, latestMonth }: BreakdownChartProps) {
  const data = await adminAPI
    .get<AnalyticsAPIResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  let labels: string[] = [];
  let values: number[] = [];
  let title = '비용 분석';

  if (data) {
    if (filter.group_by === 'team') {
      labels = (data.by_team ?? []).map((b) => b.team);
      values = (data.by_team ?? []).map((b) => Number(b.cost_usd));
      title = '팀별 비용 분석';
    } else if (filter.group_by === 'user') {
      // §60.9: 백엔드 by_user 집계 사용(이전엔 by_model 로 잘못 표시됐음).
      labels = (data.by_user ?? []).map((b) => b.user || b.email);
      values = (data.by_user ?? []).map((b) => Number(b.cost_usd));
      title = '사용자별 비용 분석';
    } else {
      labels = (data.by_model ?? []).map((b) => b.model);
      values = (data.by_model ?? []).map((b) => Number(b.cost_usd));
      title = '모델별 비용 분석';
    }
  }

  return (
    <div className="glass glass-hover rounded-apple p-4">
      <BreakdownChartClient labels={labels} values={values} title={title} />
    </div>
  );
}
