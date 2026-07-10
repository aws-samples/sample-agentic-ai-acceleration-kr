// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Area,
  AreaChart,
} from 'recharts';
import type { ChartSpec } from './types';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

const CHART_COLORS = [
  'hsl(var(--chart-1))',
  'hsl(var(--chart-2))',
  'hsl(var(--chart-3))',
  'hsl(var(--chart-4))',
  'hsl(var(--chart-5))',
];

// 툴팁 박스/텍스트를 테마 토큰으로 통일(라이트·다크 모두 정합). recharts 기본값은
// 흰 배경·검정 글씨라 다크모드에서 부조화 → card/foreground 토큰으로 강제.
const TOOLTIP_CONTENT_STYLE = {
  backgroundColor: 'hsl(var(--card))',
  border: '1px solid hsl(var(--border))',
  borderRadius: 'var(--radius)',
  boxShadow: '0 4px 12px hsl(var(--foreground) / 0.08)',
} as const;
const TOOLTIP_LABEL_STYLE = { color: 'hsl(var(--muted-foreground))' } as const;
const TOOLTIP_ITEM_STYLE = { color: 'hsl(var(--card-foreground))' } as const;
// bar 호버 커서(막대 뒤 음영) — recharts 기본 #ccc 불투명 회색은 테마 무관이라
// 두 모드 다 부조화. muted 토큰 반투명으로 은은하게(팔레트와 충돌 안 함).
const BAR_CURSOR = { fill: 'hsl(var(--muted))', fillOpacity: 0.5 } as const;
// line/area 호버 커서(세로 기준선) — border 토큰 점선.
const AXIS_CURSOR = { stroke: 'hsl(var(--muted-foreground))', strokeOpacity: 0.4, strokeDasharray: '3 3' } as const;

interface Props {
  spec: ChartSpec;
}

export function ChartRenderer({ spec }: Props) {
  return (
    <div className="glass glass-hover rounded-apple p-4 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 duration-200">
      {spec.title && (
        <h3 className="text-sm font-semibold mb-3 text-card-foreground">{spec.title}</h3>
      )}
      <div className="h-64 w-full">
        <ChartBody spec={spec} />
      </div>
    </div>
  );
}

function ChartBody({ spec }: { spec: ChartSpec }) {
  const { kind, data, encoding } = spec;
  const yKeys = Array.isArray(encoding.y) ? encoding.y : [encoding.y];

  if (kind === 'kpi') {
    return <KpiCard data={data} y={yKeys[0]} x={encoding.x} />;
  }

  if (kind === 'table') {
    return <DataTable data={data} />;
  }

  if (kind === 'image') {
    // Code Specialist 가 만든 PNG 의 presigned URL 가정
    const url = (data?.[0]?.url as string) || (data?.[0]?.image_url as string);
    if (!url) return <div className="text-sm text-muted-foreground">이미지 없음</div>;
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={url} alt={spec.title || 'chart'} className="max-w-full max-h-full" />;
  }

  if (kind === 'pie') {
    const dataKey = yKeys[0];
    return (
      <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={200}>
        <PieChart>
          <Pie
            data={data}
            dataKey={dataKey}
            nameKey={encoding.x}
            innerRadius={40}
            outerRadius={80}
            paddingAngle={2}
          >
            {data.map((_, i) => (
              <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={TOOLTIP_CONTENT_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
          />
          <Legend />
        </PieChart>
      </ResponsiveContainer>
    );
  }

  if (kind === 'line') {
    return (
      <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={200}>
        <LineChart data={data} margin={{ top: 5, right: 16, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey={encoding.x} stroke="hsl(var(--muted-foreground))" fontSize={11} />
          <YAxis stroke="hsl(var(--muted-foreground))" fontSize={11} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
          />
          {yKeys.length > 1 && <Legend />}
          {yKeys.map((k, i) => (
            <Line
              key={k}
              type="monotone"
              dataKey={k}
              stroke={CHART_COLORS[i % CHART_COLORS.length]}
              strokeWidth={2}
              dot={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  if (kind === 'area') {
    return (
      <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={200}>
        <AreaChart data={data} margin={{ top: 5, right: 16, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey={encoding.x} stroke="hsl(var(--muted-foreground))" fontSize={11} />
          <YAxis stroke="hsl(var(--muted-foreground))" fontSize={11} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
          />
          {yKeys.length > 1 && <Legend />}
          {yKeys.map((k, i) => (
            <Area
              key={k}
              type="monotone"
              dataKey={k}
              stroke={CHART_COLORS[i % CHART_COLORS.length]}
              fill={CHART_COLORS[i % CHART_COLORS.length]}
              fillOpacity={0.25}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    );
  }

  // default: bar
  return (
    <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={200}>
      <BarChart data={data} margin={{ top: 5, right: 16, bottom: 5, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
        <XAxis dataKey={encoding.x} stroke="hsl(var(--muted-foreground))" fontSize={11} />
        <YAxis stroke="hsl(var(--muted-foreground))" fontSize={11} />
        <Tooltip
          cursor={BAR_CURSOR}
          contentStyle={TOOLTIP_CONTENT_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
        />
        {yKeys.length > 1 && <Legend />}
        {yKeys.map((k, i) => (
          <Bar key={k} dataKey={k} fill={CHART_COLORS[i % CHART_COLORS.length]} radius={[4, 4, 0, 0]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function KpiCard({ data, y, x }: { data: Record<string, unknown>[]; y: string; x?: string }) {
  if (data.length === 1) {
    const v = data[0][y];
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="text-4xl font-bold">{formatNumber(v)}</div>
          {x && data[0][x] != null && (
            <div className="mt-1 text-sm text-muted-foreground">{String(data[0][x])}</div>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 h-full overflow-auto">
      {data.map((row, i) => (
        <div
          key={i}
          className="rounded-md border border-border bg-card/50 p-3 flex flex-col justify-center"
        >
          <div className="text-2xl font-semibold">{formatNumber(row[y])}</div>
          {x && row[x] != null && (
            <div className="text-xs text-muted-foreground truncate">{String(row[x])}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function DataTable({ data }: { data: Record<string, unknown>[] }) {
  if (data.length === 0) {
    return <div className="text-sm text-muted-foreground">데이터 없음</div>;
  }
  const cols = Object.keys(data[0]);
  // 숫자 컬럼 자동 판정 — 해당 컬럼 값이 (거의) 전부 number 면 우측정렬+tabular-nums.
  const numericCols = new Set(
    cols.filter((c) => {
      const vals = data.map((r) => r[c]).filter((v) => v != null);
      return vals.length > 0 && vals.every((v) => typeof v === 'number');
    }),
  );
  // 모델관리 등과 동일한 공통 Table 컴포넌트로 통일(§60) — compact 밀도(채팅 폭 절약).
  return (
    <div className="h-full overflow-auto">
      <Table density="compact">
        <THead>
          <Tr>
            {cols.map((c) => (
              <Th key={c} numeric={numericCols.has(c)}>
                {c}
              </Th>
            ))}
          </Tr>
        </THead>
        <TBody>
          {data.map((row, i) => (
            <Tr key={i}>
              {cols.map((c) => (
                <Td key={c} numeric={numericCols.has(c)}>
                  {formatCell(row[c])}
                </Td>
              ))}
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function formatNumber(v: unknown): string {
  if (typeof v === 'number') {
    if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
    if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toFixed(2);
  }
  return String(v ?? '');
}

function formatCell(v: unknown): string {
  if (v == null) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
