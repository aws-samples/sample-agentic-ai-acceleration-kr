"use client";

import React from "react";
import {
  Area,
  AreaChart,
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
} from "recharts";

import type { Chart, ChartSpec } from "@/app/types/types";
import { cn } from "@/lib/utils";
// Shared with the insights dashboard's own charts so the two cannot drift: a
// tooltip that is readable here and white-on-white there is the failure this
// prevents. The colour comments and validation figures live in chartTheme.ts.
import {
  AXIS,
  AXIS_CURSOR,
  BAR_CURSOR,
  BAR_GAP,
  compactNumber,
  GRID,
  SERIES_CAP,
  SERIES_COLORS,
  TOOLTIP_CONTENT,
  TOOLTIP_ITEM,
  TOOLTIP_LABEL,
} from "@/app/components/chartTheme";

interface Props {
  chart: Chart;
  className?: string;
}

/**
 * A chart the agent produced.
 *
 * Prefers `spec`, which redraws on every render in the current theme and never
 * expires. The image is only a fallback: it is presigned for about five minutes
 * while the object behind it lives for days, so a thread reopened later shows it
 * broken. Avoiding that is the whole reason the server forwards a spec at all.
 *
 * A chart with neither does not reach here — the server drops that frame — so the
 * null return below is a guard, not a path with a caption to write.
 */
export const ChartRenderer = React.memo<Props>(function ChartRenderer({
  chart,
  className,
}) {
  const { spec } = chart;

  if (!spec) {
    const url = chart.url;
    if (!url) return null;
    return (
      <figure className={cn("rounded-lg border border-border bg-card p-3", className)}>
        <img
          src={url}
          alt="에이전트가 생성한 차트"
          className="max-w-full rounded"
        />
        <figcaption className="mt-2 text-xxs text-muted-foreground">
          이미지 차트입니다. 링크가 만료되면 다시 질문해 주세요.
        </figcaption>
      </figure>
    );
  }

  return (
    <figure className={cn("rounded-lg border border-border bg-card p-4", className)}>
      {spec.title && (
        <figcaption className="mb-3 text-sm font-semibold text-card-foreground">
          {spec.title}
        </figcaption>
      )}
      <ChartBody spec={spec} />
    </figure>
  );
});

function ChartBody({ spec }: { spec: ChartSpec }) {
  const data = Array.isArray(spec.data) ? spec.data : [];
  if (data.length === 0) {
    return <p className="text-sm text-muted-foreground">표시할 데이터가 없습니다.</p>;
  }

  // `y` is a single key or several; the runtime sends both shapes.
  const yKeys = (Array.isArray(spec.encoding?.y)
    ? spec.encoding.y
    : [spec.encoding?.y]
  ).filter((k): k is string => typeof k === "string" && k.length > 0);
  const x = spec.encoding?.x;

  if (spec.kind === "table") return <DataTable data={data} />;
  if (spec.kind === "kpi") return <KpiCards data={data} y={yKeys[0]} x={x} />;

  if (yKeys.length === 0) {
    // Nothing to plot: fall back to the table rather than an empty axis frame,
    // so the numbers still reach the reader.
    return <DataTable data={data} />;
  }

  const series = yKeys.slice(0, SERIES_CAP);
  const dropped = yKeys.length - series.length;
  // A legend whenever more than one series shares the plot — identity must never
  // rest on colour alone. One series needs none: the title names it.
  const legend = series.length > 1 ? <Legend wrapperStyle={{ fontSize: "0.75rem" }} /> : null;

  return (
    <>
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={200}>
          {spec.kind === "pie" ? (
            <PieChart>
              <Pie
                data={data}
                dataKey={series[0]}
                nameKey={x}
                innerRadius={44}
                outerRadius={84}
                // The same 2px separation the bars get, as an angle gap.
                paddingAngle={2}
                stroke="hsl(var(--card))"
                strokeWidth={BAR_GAP}
              >
                {data.map((_, i) => (
                  <Cell key={i} fill={SERIES_COLORS[i % SERIES_CAP]} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
              />
              <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
            </PieChart>
          ) : spec.kind === "line" ? (
            <LineChart data={data} margin={{ top: 5, right: 16, bottom: 5, left: 0 }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey={x} {...AXIS} />
              <YAxis {...AXIS} tickFormatter={compactNumber} />
              <Tooltip
                cursor={AXIS_CURSOR}
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
              />
              {legend}
              {series.map((k, i) => (
                <Line
                  key={k}
                  type="monotone"
                  dataKey={k}
                  stroke={SERIES_COLORS[i % SERIES_CAP]}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "hsl(var(--card))" }}
                />
              ))}
            </LineChart>
          ) : spec.kind === "area" ? (
            <AreaChart data={data} margin={{ top: 5, right: 16, bottom: 5, left: 0 }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey={x} {...AXIS} />
              <YAxis {...AXIS} tickFormatter={compactNumber} />
              <Tooltip
                cursor={AXIS_CURSOR}
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
              />
              {legend}
              {series.map((k, i) => (
                <Area
                  key={k}
                  type="monotone"
                  dataKey={k}
                  stroke={SERIES_COLORS[i % SERIES_CAP]}
                  strokeWidth={2}
                  fill={SERIES_COLORS[i % SERIES_CAP]}
                  fillOpacity={0.22}
                />
              ))}
            </AreaChart>
          ) : (
            <BarChart
              data={data}
              margin={{ top: 5, right: 16, bottom: 5, left: 0 }}
              barGap={BAR_GAP}
            >
              <CartesianGrid {...GRID} vertical={false} />
              <XAxis dataKey={x} {...AXIS} />
              <YAxis {...AXIS} tickFormatter={compactNumber} />
              <Tooltip
                cursor={BAR_CURSOR}
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
              />
              {legend}
              {series.map((k, i) => (
                <Bar
                  key={k}
                  dataKey={k}
                  fill={SERIES_COLORS[i % SERIES_CAP]}
                  // Rounded at the data end only; the baseline stays square so the
                  // bar reads as measured from zero.
                  radius={[4, 4, 0, 0]}
                />
              ))}
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
      {dropped > 0 && (
        <p className="mt-2 text-xxs text-muted-foreground">
          계열 {dropped}개는 표시하지 않았습니다. 전체는 표로 확인하세요.
        </p>
      )}
    </>
  );
}

/**
 * The relief the palette owes a light-mode reader.
 *
 * Three of the eight series steps sit below 3:1 contrast on the light card, which
 * is allowed only if the values are also legible as text. This table is that
 * guarantee, and it doubles as the accessible view of every chart kind.
 */
function DataTable({ data }: { data: Record<string, unknown>[] }) {
  const cols = Object.keys(data[0] ?? {});
  if (cols.length === 0) {
    return <p className="text-sm text-muted-foreground">표시할 데이터가 없습니다.</p>;
  }
  // Right-align a column only when every value in it is a number, so mixed
  // columns are not silently treated as numeric.
  const numeric = new Set(
    cols.filter((c) => {
      const vals = data.map((r) => r[c]).filter((v) => v != null);
      return vals.length > 0 && vals.every((v) => typeof v === "number");
    })
  );

  return (
    <div className="max-h-64 overflow-auto">
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-card">
          <tr>
            {cols.map((c) => (
              <th
                key={c}
                scope="col"
                className={cn(
                  "border-b border-border px-2 py-1.5 font-medium text-muted-foreground",
                  numeric.has(c) ? "text-right" : "text-left"
                )}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr key={i} className="border-b border-border/50 last:border-0">
              {cols.map((c) => (
                <td
                  key={c}
                  className={cn(
                    "px-2 py-1.5 text-foreground",
                    numeric.has(c) ? "text-right tabular-nums" : "text-left"
                  )}
                >
                  {formatCell(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A headline figure, or a small row of them. No plot, so no hover layer. */
function KpiCards({
  data,
  y,
  x,
}: {
  data: Record<string, unknown>[];
  y?: string;
  x?: string;
}) {
  if (!y) return <DataTable data={data} />;

  if (data.length === 1) {
    const row = data[0];
    return (
      <div className="flex flex-col items-center justify-center py-4">
        <div className="text-3xl font-semibold tabular-nums text-foreground">
          {compactNumber(row[y])}
        </div>
        {x && row[x] != null && (
          <div className="mt-1 text-xs text-muted-foreground">{String(row[x])}</div>
        )}
      </div>
    );
  }

  return (
    <div className="grid max-h-64 grid-cols-2 gap-2 overflow-auto sm:grid-cols-3">
      {data.map((row, i) => (
        <div key={i} className="rounded-md border border-border bg-background p-2.5">
          <div className="text-lg font-semibold tabular-nums text-foreground">
            {compactNumber(row[y])}
          </div>
          {x && row[x] != null && (
            <div className="truncate text-xxs text-muted-foreground">
              {String(row[x])}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function formatCell(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
