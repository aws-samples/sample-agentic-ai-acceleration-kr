'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';
import { Bar } from 'react-chartjs-2';
import type { ModelBreakdown, TeamBreakdown } from '@/types/entities';
import { CATEGORICAL_PALETTE, useChartTheme } from '@/lib/utils/chartTheme';

ChartJS.register(CategoryScale, LinearScale, BarElement, Title, Tooltip, Legend);

interface BreakdownChartClientProps {
  labels: string[];
  values: number[];
  title: string;
}

export function BreakdownChartClient({ labels, values, title }: BreakdownChartClientProps) {
  const t = useChartTheme();
  const data = {
    labels,
    datasets: [
      {
        label: '비용 (USD)',
        data: values,
        // 막대마다 카테고리 색 — 항목 구분 또렷.
        backgroundColor: values.map((_, i) => CATEGORICAL_PALETTE[i % CATEGORICAL_PALETTE.length]),
        borderWidth: 0,
        borderRadius: 6,
      },
    ],
  };

  const options = {
    responsive: true,
    plugins: {
      legend: { display: false },
      title: { display: true, text: title, color: t.text },
      tooltip: {
        callbacks: {
          label: (ctx: import('chart.js').TooltipItem<'bar'>) =>
            `$${(ctx.parsed.y ?? 0).toFixed(4)}`,
        },
      },
    },
    scales: {
      x: {
        title: { display: true, text: '항목', color: t.textMuted },
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

  if (labels.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        데이터가 없습니다.
      </div>
    );
  }

  return <Bar data={data} options={options} />;
}

// 모델별 breakdown용 타입 헬퍼
export function modelBreakdownToChartProps(breakdowns: ModelBreakdown[]) {
  return {
    labels: breakdowns.map((b) => b.model_alias),
    values: breakdowns.map((b) => b.cost_usd),
    title: '모델별 비용 분석',
  };
}

// 팀별 breakdown용 타입 헬퍼
export function teamBreakdownToChartProps(breakdowns: TeamBreakdown[]) {
  return {
    labels: breakdowns.map((b) => b.team_name),
    values: breakdowns.map((b) => b.cost_usd),
    title: '팀별 비용 분석',
  };
}