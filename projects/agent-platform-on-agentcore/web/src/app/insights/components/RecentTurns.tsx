"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  AXIS,
  BAR_CURSOR,
  BAR_GAP,
  BAR_MAX_SIZE,
  compactNumber,
  COST_COLOR,
  GRID,
  METRIC_COLOR,
  SERIES_COLORS,
  TOOLTIP_CONTENT,
  TOOLTIP_ITEM,
  TOOLTIP_LABEL,
} from "@/app/components/chartTheme";
import type { RecordTurn } from "@/lib/insights";
import { cn } from "@/lib/utils";
import { formatMicros, formatTokens } from "@/app/insights/insightsFormat.mjs";

const STATUS_LABEL: Record<string, string> = {
  completed: "완료",
  interrupted: "중단",
  failed: "실패",
  blocked: "가드레일 차단",
  unknown: "미상",
};

/** One chart row per measured turn, oldest first so the x-axis reads as time. */
interface TurnPoint {
  /**
   * The x value. The turn id rather than its time label: several turns end in
   * the same minute, and recharts resolves "which bar is the mouse on" by the x
   * value — with duplicate labels the tooltip and the mouse-move payload settled
   * on *different* turns of the same minute (measured: tooltip on the 3,901-token
   * turn, payload on the 18,414-token one beside it). A unique key makes them
   * agree, and the tick formatter puts the time back on the axis.
   */
  turn_id: string;
  /** Position in the *table's* (newest-first) order, for the hover link. */
  row: number;
  label: string;
  at: string;
  status: string;
  input_tokens: number;
  output_tokens: number;
  /** Dollars, or null for a turn the rate card could not price — drawn as a gap. */
  cost: number | null;
}

const tooltipTurn = (payload?: ReadonlyArray<{ payload?: unknown }>) => {
  const point = payload?.[0]?.payload as TurnPoint | undefined;
  if (!point) return "";
  // Time and ending only. The model id is forty characters and pushed the tooltip
  // past the card's edge; it is in the table row that lights up under the cursor.
  return `${point.at} · ${STATUS_LABEL[point.status] ?? point.status}`;
};

const tooltipTokens = (value: unknown, name: unknown): [string, string] => [
  typeof value === "number" ? value.toLocaleString("ko-KR") : String(value ?? ""),
  String(name ?? ""),
];

/**
 * The recent turns of one agent: the table it always had, and beside it the same
 * turns as two small charts on one time axis.
 *
 * The table answers "what was this one turn"; the charts answer the question the
 * table cannot — whether the last hundred turns are getting heavier, whether one
 * of them is the outlier that explains today's cost, whether output is growing
 * faster than input. Reading those off a hundred rows of tabular numbers is
 * exactly the job a chart exists to take over.
 *
 * Two charts rather than one with two y-axes. Tokens and dollars are different
 * quantities, and a dual-axis plot invites reading the bar heights against each
 * other when they share no scale. Stacked on one x-axis and synced by `syncId`,
 * hovering a turn lights it in both — and, through `active`, in the table.
 *
 * Input and output are stacked because they sum to the turn's tokens; cost is in
 * the page-wide cost hue so it reads as the same nature as the billed columns.
 * An unpriced turn is a gap in the cost chart, not a zero: zero would be a claim.
 */
export function RecentTurns({ turns }: { turns: RecordTurn[] }) {
  const measured = turns.filter((turn) => turn.measured);
  const [active, setActive] = useState<number | null>(null);

  const points: TurnPoint[] = measured
    .map((turn, row) => ({
      turn_id: turn.turn_id,
      row,
      label: turn.ended_at.slice(5, 16).replace("T", " "),
      at: turn.ended_at.slice(0, 19).replace("T", " "),
      status: turn.status,
      input_tokens: turn.input_tokens,
      output_tokens: turn.output_tokens,
      cost: turn.model_cost_micros === null ? null : turn.model_cost_micros / 1_000_000,
    }))
    .reverse();

  const byId = new Map(points.map((point) => [point.turn_id, point]));
  const labelOf = (id: unknown) => byId.get(String(id))?.label ?? "";

  // The table is in the opposite order from the chart, so the row number travels
  // with the point and is looked up by the x value the chart reports.
  const onMove = (state: { activeLabel?: unknown; isTooltipActive?: boolean }) => {
    const point = state.isTooltipActive ? byId.get(String(state.activeLabel)) : undefined;
    setActive(point?.row ?? null);
  };
  const onLeave = () => setActive(null);

  return (
    <div className="grid grid-cols-1 gap-3 @3xl:grid-cols-2">
      <div className="max-h-72 overflow-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="px-2 py-1 text-left font-medium">시각</th>
              <th className="px-2 py-1 text-left font-medium">상태</th>
              <th className="px-2 py-1 text-left font-medium">모델</th>
              <th className="px-2 py-1 text-right font-medium">입력</th>
              <th className="px-2 py-1 text-right font-medium">출력</th>
              <th className="px-2 py-1 text-right font-medium">비용</th>
            </tr>
          </thead>
          <tbody>
            {measured.map((turn, row) => (
              <tr
                key={turn.turn_id}
                data-active={active === row || undefined}
                className={cn(
                  "border-b border-border/50 last:border-0",
                  active === row && "bg-muted/60",
                )}
              >
                <td className="whitespace-nowrap px-2 py-1 tabular-nums">
                  {turn.ended_at.slice(0, 16).replace("T", " ")}
                </td>
                <td className="px-2 py-1">{STATUS_LABEL[turn.status] ?? turn.status}</td>
                <td className="max-w-48 truncate px-2 py-1 font-mono text-xxs" title={turn.model_id ?? ""}>
                  {turn.model_id ?? "—"}
                </td>
                <td className="px-2 py-1 text-right tabular-nums">
                  {formatTokens(turn.input_tokens)}
                </td>
                <td className="px-2 py-1 text-right tabular-nums">
                  {formatTokens(turn.output_tokens)}
                </td>
                <td className="px-2 py-1 text-right tabular-nums">
                  {formatMicros(turn.model_cost_micros, {
                    unpriced: turn.model_cost_micros === null ? 1 : 0,
                  })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex min-w-0 flex-col gap-2">
        {/* Tokens on top, taller, because it carries the legend and two series. */}
        <div className="h-40 w-full">
          <ResponsiveContainer width="100%" height="100%" minWidth={0}>
            <BarChart
              data={points}
              syncId="recent-turns"
              barGap={BAR_GAP}
              margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
              onMouseMove={onMove}
              onMouseLeave={onLeave}
            >
              <CartesianGrid {...GRID} vertical={false} />
              <XAxis dataKey="turn_id" tickFormatter={labelOf} {...AXIS} interval="preserveStartEnd" />
              <YAxis {...AXIS} width={56} tickFormatter={compactNumber} />
              <Tooltip
                cursor={BAR_CURSOR}
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
                formatter={tooltipTokens}
                labelFormatter={(_, payload) => tooltipTurn(payload)}
              />
              <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
              <Bar
                dataKey="input_tokens"
                name="입력"
                stackId="tokens"
                fill={SERIES_COLORS[METRIC_COLOR.input_tokens]}
                maxBarSize={BAR_MAX_SIZE}
                isAnimationActive={false}
              />
              <Bar
                dataKey="output_tokens"
                name="출력"
                stackId="tokens"
                fill={SERIES_COLORS[METRIC_COLOR.output_tokens]}
                maxBarSize={BAR_MAX_SIZE}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="h-28 w-full">
          <ResponsiveContainer width="100%" height="100%" minWidth={0}>
            <BarChart
              data={points}
              syncId="recent-turns"
              margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
              onMouseMove={onMove}
              onMouseLeave={onLeave}
            >
              <CartesianGrid {...GRID} vertical={false} />
              <XAxis dataKey="turn_id" tickFormatter={labelOf} {...AXIS} interval="preserveStartEnd" />
              <YAxis
                {...AXIS}
                width={56}
                tickFormatter={(value: number) => `$${compactNumber(value)}`}
              />
              <Tooltip
                cursor={BAR_CURSOR}
                contentStyle={TOOLTIP_CONTENT}
                labelStyle={TOOLTIP_LABEL}
                itemStyle={TOOLTIP_ITEM}
                formatter={(value) => [
                  typeof value === "number" ? `$${value.toFixed(4)}` : "요율 미등록",
                  "비용",
                ]}
                labelFormatter={(_, payload) => tooltipTurn(payload)}
              />
              <Bar
                dataKey="cost"
                name="비용"
                fill={COST_COLOR}
                maxBarSize={BAR_MAX_SIZE}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
