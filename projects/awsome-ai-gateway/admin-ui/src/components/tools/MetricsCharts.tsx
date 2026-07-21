'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import { Line, Bar } from 'react-chartjs-2';
import { TIME_RANGES, type TimeRangeKey } from '@/lib/tool-gateway/constants';
import { useChartTheme, PRIMARY_SERIES, CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
  Filler
);

interface MetricsDataPoint {
  timestamp: string;
  value?: number;
  p50?: number;
  p90?: number;
  p99?: number;
  system_errors?: number;
  user_errors?: number;
}

interface ErrorDataPoint {
  timestamp: string;
  system_errors: number;
  user_errors: number;
}

interface ToolMetrics {
  name: string;
  label: string;
  invocations: number;
  latency: number;
  targetExec: number;
  systemErrors: number;
  userErrors: number;
  errors: number;
  errorRate: number;
  overhead: number;
}

interface AuthMetrics {
  inboundSuccess: number;
  inboundFailure: number;
  inboundFailureByType: Array<{ exceptionType: string; count: number }>;
  apiKeySuccess: number;
  apiKeyFailure: number;
  apiKeySuccessByProvider: Array<{ provider: string; count: number }>;
  apiKeyFailureByProvider: Array<{ label: string; count: number }>;
}

interface SummaryMetrics {
  total_invocations: number;
  total_system_errors: number;
  total_user_errors: number;
  total_throttles: number;
  error_rate: number;
  avg_latency: number;
  avg_target_exec: number;
}

interface MetricsResponse {
  timeRange: string;
  period: number;
  invocations: MetricsDataPoint[];
  latency: MetricsDataPoint[];
  errors: ErrorDataPoint[];
  overhead: MetricsDataPoint[];
  tools: ToolMetrics[];
  toolTrend: Record<string, number | string>[];
  toolTrendSeries: Array<{ name: string; label: string }>;
  auth: AuthMetrics;
  summary: SummaryMetrics;
  status: 'Complete' | 'NoData' | 'Unavailable' | 'Disabled';
}

export function MetricsCharts() {
  const t = useChartTheme();
  const [timeRange, setTimeRange] = useState<TimeRangeKey>('24h');
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchMetrics = async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await fetch(`/api/tools/metrics?timeRange=${timeRange}`);
        if (!response.ok) {
          throw new Error(`API error: ${response.status}`);
        }
        const result = (await response.json()) as MetricsResponse;
        setData(result);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    };

    fetchMetrics();
  }, [timeRange]);

  // CloudWatch timestamps arrive as ISO 8601 strings (Date.toISOString()), not
  // epoch numbers — parse directly (multiplying a string by 1000 yields NaN).
  const formatTime = (timestamp: string): string => {
    return new Date(timestamp).toLocaleTimeString('ko-KR', {
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        불러오는 중…
      </div>
    );
  }

  if (data?.status === 'Disabled') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway가 비활성화되어 있습니다.
      </div>
    );
  }

  if (data?.status === 'Unavailable') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway에 연결할 수 없습니다.
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-destructive">
        오류: {error}
      </div>
    );
  }

  if (!data || data.status === 'NoData') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        데이터가 없습니다.
      </div>
    );
  }

  // KPI row
  const summary = data.summary;
  const kpiCards = [
    { label: '총 호출 수', value: summary.total_invocations.toLocaleString() },
    { label: '에러율', value: `${(summary.error_rate * 100).toFixed(2)}%` },
    { label: '평균 지연시간', value: `${summary.avg_latency.toFixed(0)}ms` },
    { label: '총 스로틀', value: summary.total_throttles.toLocaleString() },
  ];

  // Invocations chart data
  const invocationLabels = data.invocations.map((d) => formatTime(d.timestamp));
  const invocationValues = data.invocations.map((d) => d.value ?? 0);

  const invocationChartData = {
    labels: invocationLabels,
    datasets: [
      {
        label: '호출 수',
        data: invocationValues,
        borderColor: PRIMARY_SERIES,
        backgroundColor: 'rgba(45, 212, 191, 0.16)',
        fill: true,
        tension: 0.3,
        pointRadius: 2,
        pointHoverRadius: 4,
      },
    ],
  };

  const invocationChartOptions = {
    responsive: true,
    plugins: {
      legend: { position: 'top' as const, labels: { color: t.text } },
      title: { display: true, text: '호출 수 추이', color: t.text },
    },
    scales: {
      x: {
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
      y: {
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
    },
  };

  // Latency chart data
  const latencyLabels = data.latency.map((d) => formatTime(d.timestamp));
  const latencyP50 = data.latency.map((d) => d.p50 ?? 0);
  const latencyP90 = data.latency.map((d) => d.p90 ?? 0);
  const latencyP99 = data.latency.map((d) => d.p99 ?? 0);

  const latencyChartData = {
    labels: latencyLabels,
    datasets: [
      {
        label: 'P50',
        data: latencyP50,
        borderColor: CATEGORICAL_PALETTE[0],
        tension: 0.3,
        pointRadius: 2,
        fill: false,
      },
      {
        label: 'P90',
        data: latencyP90,
        borderColor: CATEGORICAL_PALETTE[1],
        tension: 0.3,
        pointRadius: 2,
        fill: false,
      },
      {
        label: 'P99',
        data: latencyP99,
        borderColor: CATEGORICAL_PALETTE[2],
        tension: 0.3,
        pointRadius: 2,
        fill: false,
      },
    ],
  };

  const latencyChartOptions = {
    responsive: true,
    plugins: {
      legend: { position: 'top' as const, labels: { color: t.text } },
      title: { display: true, text: '지연시간 백분위수', color: t.text },
    },
    scales: {
      x: {
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
      y: {
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
    },
  };

  // Errors chart data
  const errorLabels = data.errors.map((d) => formatTime(d.timestamp));
  const systemErrors = data.errors.map((d) => d.system_errors);
  const userErrors = data.errors.map((d) => d.user_errors);

  const errorChartData = {
    labels: errorLabels,
    datasets: [
      {
        label: '시스템 에러',
        data: systemErrors,
        backgroundColor: CATEGORICAL_PALETTE[2],
        borderWidth: 0,
      },
      {
        label: '사용자 에러',
        data: userErrors,
        backgroundColor: CATEGORICAL_PALETTE[1],
        borderWidth: 0,
      },
    ],
  };

  const errorChartOptions = {
    responsive: true,
    plugins: {
      legend: { position: 'top' as const, labels: { color: t.text } },
      title: { display: true, text: '에러 추이', color: t.text },
    },
    scales: {
      x: {
        stacked: true,
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
      y: {
        stacked: true,
        ticks: { color: t.textMuted },
        grid: { color: t.grid },
      },
    },
  };

  return (
    <div className="space-y-6">
      {/* Time Range Selector */}
      <div className="flex gap-2">
        {Object.entries(TIME_RANGES).map(([key, config]) => (
          <button
            key={key}
            onClick={() => setTimeRange(key as TimeRangeKey)}
            className={`px-3 py-1 text-sm rounded font-medium transition ${
              timeRange === key
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80'
            }`}
          >
            {config.label}
          </button>
        ))}
      </div>

      {/* KPI Row */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        {kpiCards.map((card) => (
          <div key={card.label} className="border rounded-lg p-4 bg-card">
            <p className="text-xs font-semibold text-muted-foreground mb-1">
              {card.label}
            </p>
            <p className="text-2xl font-bold">{card.value}</p>
          </div>
        ))}
      </div>

      {/* Charts Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Invocations Chart */}
        <div className="border rounded-lg p-4 bg-card">
          {invocationValues.length > 0 ? (
            <Line data={invocationChartData} options={invocationChartOptions} />
          ) : (
            <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
              데이터가 없습니다.
            </div>
          )}
        </div>

        {/* Latency Chart */}
        <div className="border rounded-lg p-4 bg-card">
          {latencyLabels.length > 0 ? (
            <Line data={latencyChartData} options={latencyChartOptions} />
          ) : (
            <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
              데이터가 없습니다.
            </div>
          )}
        </div>

        {/* Errors Chart */}
        <div className="border rounded-lg p-4 bg-card lg:col-span-2">
          {errorLabels.length > 0 ? (
            <Bar data={errorChartData} options={errorChartOptions} />
          ) : (
            <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
              데이터가 없습니다.
            </div>
          )}
        </div>
      </div>

      {/* Tools Table */}
      <div className="border rounded-lg p-4 bg-card overflow-x-auto">
        <h3 className="text-lg font-semibold mb-4">Tool별 메트릭</h3>
        {data.tools.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="text-left py-2 px-3 font-semibold">Tool</th>
                <th className="text-right py-2 px-3 font-semibold">호출 수</th>
                <th className="text-right py-2 px-3 font-semibold">평균 지연(ms)</th>
                <th className="text-right py-2 px-3 font-semibold">에러 수</th>
                <th className="text-right py-2 px-3 font-semibold">에러율</th>
              </tr>
            </thead>
            <tbody>
              {data.tools.map((tool) => (
                <tr key={tool.name} className="border-b hover:bg-muted/50">
                  <td className="py-2 px-3">{tool.label || tool.name}</td>
                  <td className="text-right py-2 px-3">
                    {tool.invocations.toLocaleString()}
                  </td>
                  <td className="text-right py-2 px-3">
                    {tool.latency.toFixed(1)}
                  </td>
                  <td className="text-right py-2 px-3">
                    {tool.errors}
                  </td>
                  <td className="text-right py-2 px-3">
                    {(tool.errorRate * 100).toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="flex items-center justify-center h-24 text-sm text-muted-foreground">
            데이터가 없습니다.
          </div>
        )}
      </div>
    </div>
  );
}
