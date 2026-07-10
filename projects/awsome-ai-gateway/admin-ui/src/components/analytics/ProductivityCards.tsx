// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';
import { fetchProductivityAnalytics } from '@/lib/actions/productivity';
import { resolveMonth } from '@/lib/utils/period';

interface ProductivityCardsProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface MetricCardProps {
  label: string;
  value: string | number;
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

export async function ProductivityCards({ filter, latestMonth }: ProductivityCardsProps) {
  const data = await fetchProductivityAnalytics(resolveMonth(filter, latestMonth)).catch(() => null);

  if (!data) {
    return (
      <div className="glass rounded-apple p-4 text-sm text-muted-foreground">
        생산성 데이터를 불러오지 못했습니다.
      </div>
    );
  }

  const p = data.productivity;
  const r = data.roi;

  const metrics: MetricCardProps[] = [
    {
      label: '코드 생성 라인',
      value: p.total_lines_generated.toLocaleString(),
      description: `수락: ${p.total_lines_accepted.toLocaleString()} (${p.code_acceptance_rate_pct}%)`,
    },
    {
      label: '커밋 수',
      value: p.total_commits.toLocaleString(),
      description: `라인당 비용: $${r.cost_per_generated_line.toFixed(4)}`,
    },
    {
      label: 'PR (오픈 / 머지)',
      value: `${p.pr_opened} / ${p.pr_merged}`,
      description: `커밋당 비용: $${r.cost_per_commit.toFixed(4)}`,
    },
    {
      label: '활성 개발자',
      value: p.active_developers.toLocaleString(),
      description: `총 비용: $${r.total_cost_usd.toFixed(4)}`,
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
