'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import type { MonitoringOverviewResponse } from '@/lib/actions/monitoring';

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="glass glass-hover rounded-apple p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold mt-1">{value}</p>
      {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
    </div>
  );
}

export function MonitoringOverview({ data }: { data: MonitoringOverviewResponse }) {
  const t = useTranslations('monitoring');
  const h = data.last_1h;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('lastHourSummary')}</h2>
        <span className="text-xs text-muted-foreground">
          {new Date(data.timestamp).toLocaleString('ko-KR')}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
        <StatCard label={t('totalRequests')} value={h.total_requests.toLocaleString()} />
        <StatCard
          label={t('errors')}
          value={h.error_count.toLocaleString()}
          sub={`${h.error_rate_pct}%`}
        />
        <StatCard label={t('avgLatency')} value={`${h.avg_latency_ms}ms`} />
        <StatCard label={t('p95Latency')} value={`${h.p95_latency_ms}ms`} />
        <StatCard label={t('totalCost')} value={`$${h.total_cost_usd.toFixed(4)}`} />
        <StatCard label={t('activeModels')} value={String(data.active_models)} />
      </div>
    </div>
  );
}