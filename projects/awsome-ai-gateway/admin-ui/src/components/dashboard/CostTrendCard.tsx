// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 비용 추이 카드 — 일별 비용 area + 요청 수 line (dual series).
 * 실데이터: /admin/analytics 의 trends[] ({date, cost_usd, requests}).
 * recharts + chart 토큰(hsl(var(--chart-N)))으로 테마(다크/라이트) 자동 연동.
 * trends 가 비면 빈 상태 표시(가짜 데이터 없음).
 */

import { useTranslations } from 'next-intl';
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';

interface TrendPoint {
  date: string;
  cost_usd: number;
  requests: number;
}

interface CostTrendCardProps {
  trends: TrendPoint[];
}

export function CostTrendCard({ trends }: CostTrendCardProps) {
  const t = useTranslations('dashboard');
  const data = trends.map((pt) => ({
    // MM-DD 표기 (YYYY-MM-DD → MM-DD)
    label: pt.date.length >= 10 ? pt.date.slice(5) : pt.date,
    cost: Number(pt.cost_usd),
    requests: pt.requests,
  }));

  return (
    <div className="glass glass-hover rounded-apple p-5">
      <div className="text-sm font-semibold tracking-tight">{t('costTrendTitle')}</div>
      <div className="mb-3 text-xs text-muted-foreground">{t('costTrendSubtitle')}</div>

      {data.length === 0 ? (
        <div className="flex h-[180px] items-center justify-center text-xs text-muted-foreground">
          {t('costTrendEmpty')}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <ComposedChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
            <defs>
              <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="hsl(var(--chart-1))" stopOpacity={0.28} />
                <stop offset="100%" stopColor="hsl(var(--chart-1))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              minTickGap={24}
            />
            <YAxis
              yAxisId="cost"
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: number) => `$${v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: 'hsl(var(--card))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '12px',
                fontSize: '12px',
              }}
              labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
              formatter={((value: number) => [
                `$${Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })}`,
                t('costSeries'),
              ]) as never}
            />
            <Area
              yAxisId="cost"
              type="monotone"
              dataKey="cost"
              stroke="hsl(var(--chart-1))"
              strokeWidth={2.5}
              fill="url(#costFill)"
            />
            <Line
              yAxisId="cost"
              type="monotone"
              dataKey="requests"
              stroke="hsl(var(--chart-2))"
              strokeWidth={2}
              dot={false}
              // 요청 수는 비용 축과 스케일이 달라 보조 시각화 — 같은 축에 normalize 표시
              hide
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
