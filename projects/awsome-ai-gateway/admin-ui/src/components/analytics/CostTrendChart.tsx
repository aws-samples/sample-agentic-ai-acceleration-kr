// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import dynamic from 'next/dynamic';
import type { AnalyticsFilterForm } from '@/types/api';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';

interface CostTrendChartProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  trends: { date: string; cost_usd: number; requests: number }[];
}

// SSR 비활성화로 Chart.js window 참조 오류 방지
const CostTrendChartClient = dynamic(
  () =>
    import('./CostTrendChartClient').then((mod) => ({ default: mod.CostTrendChartClient })),
  { ssr: false }
);

export async function CostTrendChart({ filter, latestMonth }: CostTrendChartProps) {
  const data = await adminAPI
    .get<AnalyticsAPIResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  const trends = (data?.trends ?? []).map((t) => ({
    date: t.date,
    cost_usd: Number(t.cost_usd),
  }));

  return (
    <div className="glass glass-hover rounded-apple p-4">
      <CostTrendChartClient trends={trends} />
    </div>
  );
}
