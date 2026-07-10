// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';

interface ROIMetricsCardsProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  period: string;
  currency: string;
  cost_summary: {
    total_requests: number;
    total_tokens: number;
    total_cost_usd: number;
    active_users: number;
    avg_cost_per_user_usd: number;
  };
  by_model: { model: string; requests: number; cost_usd: number }[];
  by_team: { team: string; team_id: string; cost_usd: number; active_users: number }[];
  trends: { date: string; cost_usd: number; requests: number }[];
}

interface MetricCardProps {
  label: string;
  value: string;
  description?: string;
}

function MetricCard({ label, value, description }: MetricCardProps) {
  return (
    <div className="glass glass-hover rounded-apple p-4 flex flex-col gap-2">
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
    </div>
  );
}

export async function ROIMetricsCards({ filter, latestMonth }: ROIMetricsCardsProps) {
  const data = await adminAPI
    .get<AnalyticsAPIResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  if (!data) {
    return (
      <div className="glass rounded-apple p-4 text-sm text-muted-foreground">
        분석 데이터를 불러오지 못했습니다.
      </div>
    );
  }

  const summary = data.cost_summary;
  const totalCost = Number(summary.total_cost_usd ?? 0);
  const avgCostPerUser = Number(summary.avg_cost_per_user_usd ?? 0);

  const metrics: MetricCardProps[] = [
    {
      label: '총 요청 수',
      value: summary.total_requests.toLocaleString(),
      description: '기간 내 총 API 요청 수',
    },
    {
      label: '총 토큰 수',
      value: summary.total_tokens.toLocaleString(),
      description: '기간 내 소비된 총 토큰 수',
    },
    {
      label: '총 비용',
      value: `$${totalCost.toFixed(4)}`,
      description: '기간 내 총 USD 비용',
    },
    {
      label: '사용자당 평균 비용',
      value: `$${avgCostPerUser.toFixed(4)}`,
      description: `활성 사용자 ${summary.active_users}명 기준`,
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {metrics.map((metric) => (
        <MetricCard key={metric.label} {...metric} />
      ))}
    </div>
  );
}
