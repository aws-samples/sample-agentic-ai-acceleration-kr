'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo } from 'react';
import {
  Chart as ChartJS,
  ArcElement,
  Tooltip,
  Legend,
} from 'chart.js';
import { Doughnut } from 'react-chartjs-2';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { labelFor } from '@/lib/utils/modelLabel';

ChartJS.register(ArcElement, Tooltip, Legend);

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  data: ClientShareResponse;
}

export function ClientShareDonutClient({ data }: Props) {
  const chartData = useMemo(
    () => ({
      labels: data.clients.map((c) => labelFor(c.client)),
      datasets: [
        {
          data: data.clients.map((c) => c.cost_usd),
          backgroundColor: data.clients.map((_, i) => COLORS[i % COLORS.length]),
          borderWidth: 0,
          spacing: 0,
          hoverOffset: 14,
        },
      ],
    }),
    [data],
  );

  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      cutout: '62%',
      animation: { animateRotate: true, animateScale: false },
      plugins: {
        legend: { display: true, position: 'bottom' as const },
        tooltip: {
          callbacks: {
            // Use the backend-computed share_pct (authoritative) rather than
            // recomputing from cost/total, which can drift due to rounding.
            label: (ctx: import('chart.js').TooltipItem<'doughnut'>) => {
              const item = data.clients[ctx.dataIndex];
              if (!item) return '';
              return `${labelFor(item.client)}: $${item.cost_usd.toFixed(2)} (${item.share_pct.toFixed(1)}%)`;
            },
            // Extra line: server-side web searches for this client (attribution metric).
            afterLabel: (ctx: import('chart.js').TooltipItem<'doughnut'>) => {
              const item = data.clients[ctx.dataIndex];
              if (!item || !item.web_search_count) return '';
              return `웹서치 ${item.web_search_count.toLocaleString()}회`;
            },
          },
        },
      },
    }),
    [data],
  );

  if (!data.clients.length) {
    return (
      <div className="flex items-center justify-center h-64 text-sm text-muted-foreground">
        이 기간에 사용 기록이 없습니다.
      </div>
    );
  }

  return (
    <div className="relative h-64">
      <Doughnut data={chartData} options={options} />
    </div>
  );
}
