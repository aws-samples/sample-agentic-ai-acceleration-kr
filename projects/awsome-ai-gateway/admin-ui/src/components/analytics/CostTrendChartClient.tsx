'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';
import { Line } from 'react-chartjs-2';
import type { TrendDataPoint } from '@/types/entities';
import { PRIMARY_SERIES, useChartTheme } from '@/lib/utils/chartTheme';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend
);

interface CostTrendChartClientProps {
  trends: TrendDataPoint[];
}

export function CostTrendChartClient({ trends }: CostTrendChartClientProps) {
  const t = useChartTheme();
  const labels = trends.map((d) => d.date);
  const values = trends.map((d) => d.cost_usd);

  const data = {
    labels,
    datasets: [
      {
        label: '비용 (USD)',
        data: values,
        borderColor: PRIMARY_SERIES,
        backgroundColor: 'rgba(45, 212, 191, 0.16)',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointHoverRadius: 5,
      },
    ],
  };

  const options = {
    responsive: true,
    plugins: {
      legend: { position: 'top' as const, labels: { color: t.text } },
      title: { display: true, text: '비용 추이', color: t.text },
      tooltip: {
        callbacks: {
          label: (ctx: import('chart.js').TooltipItem<'line'>) =>
            `$${(ctx.parsed.y ?? 0).toFixed(4)}`,
        },
      },
    },
    scales: {
      x: {
        title: { display: true, text: '날짜', color: t.textMuted },
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
      y: {
        title: { display: true, text: 'USD', color: t.textMuted },
        ticks: {
          color: t.textMuted,
          callback: (value: string | number) => `$${Number(value).toFixed(2)}`,
        },
        grid: { color: t.grid },
      },
    },
  };

  if (trends.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        데이터가 없습니다.
      </div>
    );
  }

  return <Line data={data} options={options} />;
}