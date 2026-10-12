"use client";

import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  Treemap,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import {
  AREA_OPACITY,
  AXIS,
  AXIS_CURSOR,
  BAR_CURSOR,
  BAR_GAP,
  BAR_MAX_SIZE,
  compactNumber,
  COST_COLOR,
  GRID,
  METRIC_COLOR,
  MUTED_MARK,
  SERIES_CAP,
  SERIES_COLORS,
  TOOLTIP_CONTENT,
  TOOLTIP_ITEM,
  TOOLTIP_LABEL,
} from "@/app/components/chartTheme";
import { cn } from "@/lib/utils";
import { shareOf, topNWithOther } from "@/app/insights/insightsFormat.mjs";

/**
 * The charts this dashboard owns.
 *
 * Separate from `ChartRenderer` on purpose. That component draws whatever spec an
 * agent produced, and its spec schema is a runtime contract — widening it with
 * stacking, horizontal bars and emphasis to serve one page would grow that
 * contract for every future agent. These are fixed charts over one known shape,
 * so they are written straight against recharts and share only the theme module,
 * which is what keeps both correct in light and dark.
 */

/** No data is a sentence, never an empty axis frame. */
export function EmptyPlot({ label }: { label: string }) {
  return <p className="py-6 text-center text-xs text-muted-foreground">{label}</p>;
}

/** A day as `dailySeries` shapes it. `label` is the axis, `date` the tooltip. */
interface DailyRow {
  date: string;
  label: string;
}

/** The full date, so a tooltip never reads "8/14" with no year. */
const tooltipDate = (payload?: ReadonlyArray<{ payload?: unknown }>) =>
  (payload?.[0]?.payload as DailyRow | undefined)?.date ?? "";

/**
 * A tooltip row: the exact value, thousands-separated, beside the series' Korean
 * name.
 *
 * Both halves are here because recharts falls back to the raw `dataKey` when a
 * series carries no `name` — measured on the built page, the hero sparkline read
 * "turns : 58" — and to an unseparated number when there is no formatter. Compact
 * figures belong on the axis; the tooltip is where a reader checks the real one.
 */
const tooltipValue = (value: unknown, name: unknown): [string, string] => [
  typeof value === "number" ? value.toLocaleString("ko-KR") : String(value ?? ""),
  String(name ?? ""),
];

/**
 * The vertical inset a sparkline's plot needs, in px, on both edges.
 *
 * `recharts` puts its baseline at the bottom of the plot area and the surface
 * clips (`overflow: hidden`), so with no bottom margin a series' minimum was
 * drawn *on* the clip edge and lost the lower half of its 2px stroke — measured
 * on the built page, every tile's curve ended at exactly the svg's own bottom
 * (521.3 against 521.3). A series that is flat at zero, like a week with no
 * failed turns, came out as a 1px rule glued to the tile's bottom padding rather
 * than as a line.
 *
 * 4 and not 1: the hover dot is r=3 with a 2px surface ring, so it reaches 4px
 * past the point it marks, and the extremes are exactly where a reader hovers.
 * Costs 8px of a 40px box, which a shape-only chart can spare.
 */
const SPARK_INSET = 4;

/**
 * A stat tile's trend, at the size where a shape is all it can carry.
 *
 * No axes and no grid: the tile's value is the figure and this only has to say
 * rising, spiky or flat. Every value is also in the trend widget and the
 * leaderboard table, so the tooltip enhances rather than gates — and it escapes
 * the viewbox vertically because a 40px plot has nowhere to put one inside.
 */
export function Sparkline({
  data,
  dataKey,
  name,
  colorIndex = 0,
}: {
  data: Array<Record<string, unknown>>;
  dataKey: string;
  name: string;
  colorIndex?: number;
}) {
  // One point is not a trend; the tile's number already says it.
  if (data.length < 2) return null;
  const color = SERIES_COLORS[colorIndex];
  const gradientId = `spark-${dataKey}-${colorIndex}`;

  return (
    // `h-12`, so the inset is paid for out of the box rather than out of the
    // plot: at `h-10` the same margins left 32px of amplitude and the slope is
    // the whole message here.
    <div className="h-12 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <AreaChart
          data={data}
          margin={{ top: SPARK_INSET, right: 0, bottom: SPARK_INSET, left: 0 }}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={AREA_OPACITY * 2} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            allowEscapeViewBox={{ x: false, y: true }}
            formatter={tooltipValue}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          <Area
            type="monotone"
            dataKey={dataKey}
            name={name}
            stroke={color}
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            // `connectNulls` stays off: an unmeasured day is a hole in the shape,
            // not a straight line drawn through it.
            connectNulls={false}
            dot={false}
            activeDot={{ r: 3, strokeWidth: 2, stroke: "hsl(var(--card))" }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Magnitude over time, one series. Columns, because each day is a count. */
export function DailyColumns({
  data,
  dataKey,
  name,
  colorIndex = 0,
}: {
  data: Array<Record<string, unknown>>;
  dataKey: string;
  name: string;
  colorIndex?: number;
}) {
  return (
    // Tall enough to include the x-axis band: a height that fits only the plot
    // gives the card a nested scrollbar for the labels.
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
          <YAxis {...AXIS} width={56} tickFormatter={compactNumber} />
          <Tooltip
            cursor={BAR_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={tooltipValue}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          <Bar
            dataKey={dataKey}
            name={name}
            fill={SERIES_COLORS[colorIndex]}
            maxBarSize={BAR_MAX_SIZE}
            // Rounded at the data end only; the baseline stays square so the bar
            // reads as measured from zero.
            radius={[4, 4, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * One series over time as a filled line, for a count that is read as a trend
 * — how many people, how many interventions — rather than day against day.
 *
 * A line rather than columns: the reader asks "rising or falling", and thirty
 * columns spend their ink on the gaps between days. The fill is faint so the
 * line stays the mark; a day with no value is a hole, not a line drawn through.
 */
export function DailyArea({
  data,
  dataKey,
  name,
  colorIndex = 0,
}: {
  data: Array<Record<string, unknown>>;
  dataKey: string;
  name: string;
  colorIndex?: number;
}) {
  const color = SERIES_COLORS[colorIndex];
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
          <YAxis {...AXIS} width={56} tickFormatter={compactNumber} allowDecimals={false} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={tooltipValue}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          {/* Straight segments with a dot per day: a smoothed curve between two
              whole counts draws a hump that no day had. */}
          <Area
            type="linear"
            dataKey={dataKey}
            name={name}
            stroke={color}
            strokeWidth={2}
            fill={color}
            fillOpacity={AREA_OPACITY}
            connectNulls={false}
            dot={{ r: 3, fill: color, strokeWidth: 2, stroke: "hsl(var(--card))" }}
            activeDot={{ r: 5, strokeWidth: 2, stroke: "hsl(var(--card))" }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Several series over time, overlaid on one shared axis — deliberately *not*
 * stacked.
 *
 * For quantities that must be compared, not summed: cache reads against cache
 * writes answer "are we reading back more than we write" — is the cache paying
 * off — and that reads only on a common y-axis. Stacking would state a total that
 * means nothing (a token written then read is counted in both, at different
 * rates); two separate plots would each auto-scale to full height and hide the
 * very ratio that matters. Lines, not filled areas, so the larger series never
 * occludes the smaller one sharing the frame.
 */
export function OverlaidDailyLines({
  data,
  series,
}: {
  data: Array<Record<string, unknown>>;
  series: Array<{ key: string; name: string; colorIndex: number }>;
}) {
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
          <YAxis {...AXIS} width={56} tickFormatter={compactNumber} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={tooltipValue}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
          {series.map((entry) => (
            <Line
              key={entry.key}
              type="monotone"
              dataKey={entry.key}
              name={entry.name}
              stroke={SERIES_COLORS[entry.colorIndex]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 3, strokeWidth: 2, stroke: "hsl(var(--card))" }}
              // An unmeasured day is a gap, not a line drawn straight through it.
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Part-to-whole over time. Two series that genuinely sum — input plus output is
 * the turn's tokens — so the stack states a total rather than inventing one.
 */
export function StackedDailyArea({
  data,
  series,
}: {
  data: Array<Record<string, unknown>>;
  series: Array<{ key: string; name: string; colorIndex: number }>;
}) {
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
          <YAxis {...AXIS} width={56} tickFormatter={compactNumber} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={tooltipValue}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          {/* Several series share the plot, so identity never rests on colour
              alone — the legend names each band. Three of them now: cached reads
              are stacked beside input and output because they are prompt tokens
              that were really sent and really billed, at a tenth of the rate. */}
          <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
          {series.map((entry) => (
            <Area
              key={entry.key}
              type="monotone"
              stackId="tokens"
              dataKey={entry.key}
              name={entry.name}
              stroke={SERIES_COLORS[entry.colorIndex]}
              strokeWidth={2}
              fill={SERIES_COLORS[entry.colorIndex]}
              fillOpacity={AREA_OPACITY}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Counts or dollars per day, stacked by entity — for quantities that genuinely
 * sum: every team's spend plus the unattributed remainder is the day's spend.
 *
 * A series carries either a palette slot or an explicit colour, so the
 * remainder can wear `MUTED_MARK` and never read as one more team. Only the top
 * segment of a day is rounded, which recharts cannot know, so none is.
 */
export function StackedDailyColumns({
  data,
  series,
  format,
  empty = "이 기간 기록이 없습니다.",
  integer = false,
}: {
  data: Array<Record<string, unknown>>;
  series: Array<{ key: string; name: string; colorIndex?: number | null; color?: string }>;
  /** Tooltip formatter for one value, e.g. dollars; axis stays compact. */
  format?: (value: number) => string;
  empty?: string;
  /** Counts: whole-number ticks only, so a 3-denial day is not read on a 0.75 grid. */
  integer?: boolean;
}) {
  const total = data.reduce(
    (sum, row) => sum + series.reduce((n, s) => n + Number(row[s.key] || 0), 0),
    0,
  );
  if (data.length === 0 || total <= 0) return <EmptyPlot label={empty} />;
  return (
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
          <YAxis {...AXIS} width={56} tickFormatter={compactNumber} allowDecimals={!integer} />
          <Tooltip
            cursor={BAR_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={(value, name) =>
              format ? [format(Number(value)), String(name)] : tooltipValue(value, name)
            }
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
          {series.map((entry) => (
            <Bar
              key={entry.key}
              dataKey={entry.key}
              name={entry.name}
              stackId="day"
              fill={entry.color ?? SERIES_COLORS[(entry.colorIndex ?? 0) % SERIES_CAP]}
              maxBarSize={BAR_MAX_SIZE}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * One entity's daily value against a line it should stay under — a team's
 * spend against its daily alert — at small-multiple size.
 *
 * Columns, not a line: each day is judged on its own, and a line between two
 * days would draw a slope through a threshold neither day crossed. A column
 * that reached the line takes the alert hue; the rest stay in the series hue.
 * No y-axis: the threshold is the scale, stated in the caption.
 */
export function ThresholdColumns({
  data,
  threshold,
  colorIndex = 0,
  format,
}: {
  data: Array<{ label: string; date: string; cost: number; over: boolean }>;
  threshold: number | null;
  colorIndex?: number;
  format: (value: number) => string;
}) {
  return (
    <div className="h-20 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <BarChart data={data} margin={{ top: 6, right: 2, bottom: 0, left: 2 }}>
          <XAxis dataKey="label" hide />
          {/* The domain reaches the threshold, so an idle team still shows where its line sits. */}
          <YAxis hide domain={[0, (max: number) => Math.max(max, threshold ?? 0) * 1.1 || 1]} />
          <Tooltip
            cursor={BAR_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            allowEscapeViewBox={{ x: false, y: true }}
            formatter={(value) => [format(Number(value)), "모델 비용"]}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          {threshold !== null && (
            <ReferenceLine
              y={threshold}
              stroke={SERIES_COLORS[METRIC_COLOR.failed_turns]}
              strokeDasharray="4 3"
              ifOverflow="extendDomain"
            />
          )}
          <Bar dataKey="cost" maxBarSize={14} radius={[2, 2, 0, 0]} isAnimationActive={false}>
            {data.map((point) => (
              <Cell
                key={point.date}
                fill={point.over ? SERIES_COLORS[METRIC_COLOR.failed_turns] : SERIES_COLORS[colorIndex % SERIES_CAP]}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface RankedEntry {
  name: string;
  value: number;
  /** Rendered at the bar's end. Pre-formatted, because the unit differs per list. */
  display: string;
  /** Secondary text after the value, e.g. "3개 에이전트". */
  meta?: string;
  /** Set by `topNWithOther` on the folded slot. */
  folded?: number;
  /** Identity, for emphasis and click-through. */
  id?: string;
}

/**
 * A ranked list as horizontal bars.
 *
 * Horizontal because the categories are long Korean names, and plain HTML rather
 * than recharts because that puts the value *outside* the bar end, where a short
 * bar can never clip it. Every value is directly labelled, so the list is its own
 * table view.
 *
 * One hue for the whole list unless `emphasisId` is set: bar length already
 * encodes magnitude, and shading each bar darker-where-bigger would spend the
 * only free channel restating it. With `emphasisId` the selected entity takes the
 * accent and the rest go grey, which is the honest form of "this one".
 */
export function RankedBars({
  entries,
  emphasisId,
  onSelect,
  colorIndex = 0,
  color,
  empty = "이 기간에 쓰인 적이 없습니다.",
}: {
  entries: RankedEntry[];
  emphasisId?: string | null;
  onSelect?: (id: string) => void;
  colorIndex?: number;
  /** An explicit bar colour, for a hue outside SERIES_COLORS (e.g. COST_COLOR). */
  color?: string;
  empty?: string;
}) {
  if (entries.length === 0) return <EmptyPlot label={empty} />;
  const barColor = color ?? SERIES_COLORS[colorIndex];
  const max = Math.max(...entries.map((entry) => entry.value), 0);

  return (
    <ul className="space-y-1.5">
      {entries.map((entry) => {
        const share = shareOf(entry.value, max);
        const emphasised = !emphasisId || entry.id === emphasisId;
        const interactive = Boolean(onSelect && entry.id);
        const Row = interactive ? "button" : "div";

        return (
          <li key={entry.id ?? entry.name}>
            <Row
              {...(interactive
                ? { type: "button" as const, onClick: () => onSelect?.(entry.id as string) }
                : {})}
              className={cn(
                "flex w-full items-center gap-2 rounded px-1 py-0.5 text-left",
                interactive && "hover:bg-muted/50",
              )}
            >
              <span
                className="w-28 shrink-0 truncate text-xs @2xl:w-44"
                title={entry.name}
              >
                {entry.name}
                {entry.folded ? (
                  <span className="text-muted-foreground"> ({entry.folded}개)</span>
                ) : null}
              </span>
              {/* The track is one step off the surface, so a bar with nothing in
                  it still reads as a bar rather than as missing. */}
              <span className="h-2.5 min-w-0 flex-1 rounded-sm bg-muted">
                <span
                  className="block h-full rounded-r-[4px]"
                  style={{
                    // A non-zero value keeps a sliver, so "small" never looks
                    // like "none".
                    width: `${Math.max(share * 100, entry.value > 0 ? 1.5 : 0)}%`,
                    backgroundColor: emphasised ? barColor : MUTED_MARK,
                  }}
                />
              </span>
              <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                {entry.display}
                {entry.meta && <span className="ml-1 text-xxs opacity-70">{entry.meta}</span>}
              </span>
            </Row>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * One 100% stacked bar plus its legend — part-to-whole, at a glance.
 *
 * The separation between segments is a 2px gap in the surface colour, not a
 * border around each one: a stroke would add ink that is not data. The legend
 * beneath carries the exact figures, so nothing is gated behind hover.
 */
export function ShareBar({
  segments,
  footnote,
}: {
  segments: Array<{ name: string; value: number; display: string }>;
  footnote?: ReactNode;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  if (segments.length === 0 || total <= 0) {
    return <EmptyPlot label="구성별로 나눌 금액이 없습니다." />;
  }

  return (
    <div className="space-y-2">
      <div className="flex h-4 w-full overflow-hidden" style={{ gap: BAR_GAP }}>
        {segments.map((segment, index) => (
          <span
            key={segment.name}
            className="h-full first:rounded-l-[4px] last:rounded-r-[4px]"
            style={{
              width: `${shareOf(segment.value, total) * 100}%`,
              backgroundColor: SERIES_COLORS[index % SERIES_COLORS.length],
            }}
            title={`${segment.name} ${segment.display}`}
          />
        ))}
      </div>
      <ul className="grid grid-cols-1 gap-x-3 gap-y-1 @md:grid-cols-2 @3xl:grid-cols-3">
        {segments.map((segment, index) => (
          <li key={segment.name} className="flex min-w-0 items-center gap-1.5 text-xs">
            {/* The swatch carries identity; the label stays in text ink, which a
                light categorical hue cannot survive as. */}
            <span
              className="size-2 shrink-0 rounded-sm"
              style={{ backgroundColor: SERIES_COLORS[index % SERIES_COLORS.length] }}
            />
            <span className="min-w-0 truncate text-muted-foreground">{segment.name}</span>
            <span className="ml-auto shrink-0 tabular-nums">{segment.display}</span>
          </li>
        ))}
      </ul>
      {footnote && <p className="text-xxs text-muted-foreground">{footnote}</p>}
    </div>
  );
}

/** A share as a whole percent, with "<1%" so a real sliver never reads as none. */
const shareLabel = (share: number) =>
  share > 0 && share < 0.005 ? "<1%" : `${Math.round(share * 100)}%`;

/**
 * Part-to-whole as a donut plus the same legend `ShareBar` carries.
 *
 * A donut rather than a second ranked list: the ranked bars beside it already
 * answer "which is biggest", and this answers "what share of the whole" — the
 * question a per-tool breakdown is actually asked. The hole keeps the ink down
 * and leaves the centre for the total, so the figure the slices divide is on the
 * plot rather than in the reader's head.
 *
 * Past `SERIES_CAP` slices the tail folds into "기타" (`topNWithOther`): a ninth
 * hue would not be distinguishable, and a sliver is not a reading. The legend
 * beneath carries exact figures, so nothing is gated behind hover.
 */
export function DonutShare({
  segments,
  totalLabel,
  format,
  empty = "이 기간에 호출된 툴이 없습니다.",
}: {
  /**
   * With `colorIndex` on every segment the caller owns order, folding and hue —
   * for a fixed set of entities whose colour must not move when a window change
   * reorders them by size. Without it the tail folds by size here.
   */
  segments: Array<{ name: string; value: number; colorIndex?: number }>;
  /** Unit for the centre total, e.g. "회". */
  totalLabel?: string;
  /** Formats the centre total, legend values and tooltip, e.g. dollars. */
  format?: (value: number) => string;
  empty?: string;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  if (segments.length === 0 || total <= 0) return <EmptyPlot label={empty} />;
  const fixed = segments.every((segment) => segment.colorIndex !== undefined);
  const data = (fixed ? segments : topNWithOther(segments, SERIES_CAP)) as Array<{
    name: string;
    value: number;
    folded?: number;
    colorIndex?: number;
  }>;
  const colorOf = (index: number) =>
    SERIES_COLORS[(data[index].colorIndex ?? index) % SERIES_CAP];
  const show = (value: number) =>
    format ? format(value) : `${value.toLocaleString("ko-KR")}${totalLabel ?? ""}`;

  return (
    <div className="flex flex-col gap-2 @md:flex-row @md:items-center">
      <div className="relative mx-auto size-40 shrink-0">
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              innerRadius={52}
              outerRadius={76}
              // One slice is the whole ring; a gap would cut it at 3 o'clock.
              paddingAngle={data.length > 1 ? 2 : 0}
              stroke={data.length > 1 ? "hsl(var(--card))" : "none"}
              strokeWidth={BAR_GAP}
              isAnimationActive={false}
            >
              {data.map((segment, index) => (
                <Cell key={segment.name} fill={colorOf(index)} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={TOOLTIP_CONTENT}
              labelStyle={TOOLTIP_LABEL}
              itemStyle={TOOLTIP_ITEM}
              // Value and the slice's name: returning an empty second element
              // dropped the tool name and left a bare count on hover.
              formatter={(value, name) => [show(Number(value)), String(name)]}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-base font-semibold tabular-nums">
            {format ? format(total) : total.toLocaleString("ko-KR")}
          </span>
          {totalLabel && <span className="text-xxs text-muted-foreground">{totalLabel}</span>}
        </div>
      </div>
      <ul className="min-w-0 flex-1 space-y-1">
        {data.map((segment, index) => (
          <li key={segment.name} className="flex min-w-0 items-center gap-1.5 text-xs">
            <span
              className="size-2 shrink-0 rounded-sm"
              style={{ backgroundColor: colorOf(index) }}
            />
            <span className="min-w-0 truncate text-muted-foreground" title={segment.name}>
              {segment.name}
              {segment.folded ? ` (${segment.folded}개)` : ""}
            </span>
            <span className="ml-auto shrink-0 tabular-nums">
              {format ? format(segment.value) : segment.value.toLocaleString("ko-KR")}
            </span>
            <span className="w-9 shrink-0 text-right text-xxs tabular-nums text-muted-foreground">
              {shareLabel(shareOf(segment.value, total))}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface TreemapEntry {
  [key: string]: unknown;
  name: string;
  value: number;
  /** Shown under the name inside the tile, pre-formatted. */
  display: string;
  id?: string;
  /** Grey instead of the hue: a remainder that is no one entity (e.g. unattributed). */
  muted?: boolean;
}

/**
 * Part-to-whole across many named entities, as area.
 *
 * For a list too long for a donut's eight hues — every agent on the platform —
 * where the question is still "who is the bulk of it". One hue: the tile's area
 * is the value, so colouring by entity would spend a channel on identity the
 * label inside the tile already carries. The 2px surface gap separates tiles.
 * Tiles too small for their label are left unlabelled and named on hover; the
 * table beside it (leaderboard) carries every exact figure.
 */
export function TreemapShare({
  entries,
  color = COST_COLOR,
  empty = "이 기간에 비용이 기록된 에이전트가 없습니다.",
}: {
  entries: TreemapEntry[];
  color?: string;
  empty?: string;
}) {
  const data = entries.filter((entry) => entry.value > 0);
  if (data.length === 0) return <EmptyPlot label={empty} />;
  const total = data.reduce((sum, entry) => sum + entry.value, 0);

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <Treemap
          data={data}
          dataKey="value"
          nameKey="name"
          aspectRatio={4 / 3}
          isAnimationActive={false}
          content={(props: TreemapTileProps) => (
            <TreemapTile {...props} color={color} total={total} />
          )}
        >
          <Tooltip
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            formatter={(_value, _name, item) => {
              const entry = item?.payload as TreemapEntry | undefined;
              return [
                `${entry?.display ?? ""} · ${shareLabel(shareOf(entry?.value ?? 0, total))}`,
                entry?.name ?? "",
              ];
            }}
          />
        </Treemap>
      </ResponsiveContainer>
    </div>
  );
}

interface TreemapTileProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  depth?: number;
  name?: string;
  value?: number;
  display?: string;
  muted?: boolean;
}

function TreemapTile({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  depth,
  name,
  value = 0,
  display,
  muted,
  color,
  total,
}: TreemapTileProps & { color: string; total: number }) {
  // The root node spans the whole plot; only leaves are drawn.
  if (depth === 0) return null;
  const roomy = width > 72 && height > 36;
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={4}
        fill={muted ? MUTED_MARK : color}
        // A tint, so the label inside can wear text ink in both themes rather
        // than white on a hue that is light in dark mode. One opacity for all:
        // bigger tiles a step darker would restate the area.
        fillOpacity={0.3}
        stroke="hsl(var(--card))"
        strokeWidth={BAR_GAP}
      />
      {roomy && (
        <>
          <text x={x + 8} y={y + 18} fill="hsl(var(--foreground))" fontSize={12} fontWeight={600}>
            {clip(name ?? "", width)}
          </text>
          <text x={x + 8} y={y + 34} fill="hsl(var(--muted-foreground))" fontSize={11}>
            {display} · {shareLabel(shareOf(value, total))}
          </text>
        </>
      )}
    </g>
  );
}

/** A label cut to what fits a tile, at ~7px a character. */
const clip = (text: string, width: number) => {
  const fits = Math.max(Math.floor((width - 16) / 7), 1);
  return text.length > fits ? `${text.slice(0, Math.max(fits - 1, 1))}…` : text;
};

export interface ScatterEntry {
  name: string;
  x: number;
  y: number;
  id?: string;
}

/**
 * Two measures of the same entity against each other, one dot per entity.
 *
 * For the question two ranked lists side by side make the reader answer by eye
 * — whose few turns are expensive — the dot above the crowd answers it. Each
 * axis has its own scale and unit; neither is a second y-axis over one x.
 */
export function ScatterPlot({
  entries,
  xName,
  yName,
  colorIndex = 0,
  empty = "표시할 값이 없습니다.",
}: {
  entries: ScatterEntry[];
  xName: string;
  yName: string;
  colorIndex?: number;
  empty?: string;
}) {
  if (entries.length === 0) return <EmptyPlot label={empty} />;
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <ScatterChart margin={{ top: 8, right: 12, bottom: 16, left: 0 }}>
          <CartesianGrid {...GRID} />
          <XAxis
            type="number"
            dataKey="x"
            name={xName}
            {...AXIS}
            tickFormatter={compactNumber}
            label={{ value: xName, position: "insideBottom", offset: -8, fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          />
          <YAxis
            type="number"
            dataKey="y"
            name={yName}
            {...AXIS}
            width={56}
            tickFormatter={compactNumber}
          />
          {/* A fixed dot size: a third channel by area would be a bubble chart,
              and this has no third measure. */}
          <ZAxis range={[64, 64]} />
          <Tooltip
            cursor={AXIS_CURSOR}
            contentStyle={TOOLTIP_CONTENT}
            labelStyle={TOOLTIP_LABEL}
            itemStyle={TOOLTIP_ITEM}
            content={({ active, payload }) => {
              const entry = payload?.[0]?.payload as ScatterEntry | undefined;
              if (!active || !entry) return null;
              return (
                <div style={{ ...TOOLTIP_CONTENT, padding: "8px 10px" }}>
                  <div style={TOOLTIP_LABEL}>{entry.name}</div>
                  <div style={TOOLTIP_ITEM}>
                    {xName} {entry.x.toLocaleString("ko-KR")}
                  </div>
                  <div style={TOOLTIP_ITEM}>
                    {yName} {entry.y.toLocaleString("ko-KR")}
                  </div>
                </div>
              );
            }}
          />
          <Scatter
            data={entries}
            fill={SERIES_COLORS[colorIndex]}
            fillOpacity={0.8}
            stroke="hsl(var(--card))"
            strokeWidth={2}
            isAnimationActive={false}
          />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Billed cost as columns over time.
 *
 * Its own component rather than a `DailyColumns` call: Cost Explorer's rows are a
 * different shape — a date that can be null, dollars on the axis, and no token
 * gapping to do.
 */
export function CostColumns({
  data,
}: {
  data: Array<{ label: string; date: string; cost: number }>;
}) {
  if (data.length === 0) return <EmptyPlot label="일별 청구액이 아직 없습니다." />;

  return (
    <div className="h-52 w-full">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid {...GRID} vertical={false} />
          <XAxis dataKey="label" {...AXIS} interval="preserveStartEnd" />
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
              typeof value === "number" ? `$${value.toFixed(4)}` : String(value),
              "청구액",
            ]}
            labelFormatter={(_, payload) => tooltipDate(payload)}
          />
          <Bar
            dataKey="cost"
            name="청구액"
            fill={COST_COLOR}
            maxBarSize={BAR_MAX_SIZE}
            radius={[4, 4, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * A plot and its heading, at the size the dashboard uses everywhere.
 *
 * The heading is a label rather than a title: small, letterspaced and in muted
 * ink. That is what puts it on its own tier between the card's own 14px title and
 * the figures below it — as `text-xs font-semibold` in body ink it measured the
 * same weight as the numbers it was introducing, and a card holding four of them
 * read as one undifferentiated column.
 *
 * `actions` is for a control that belongs to this section only (a fold toggle, a
 * retry); page-level controls stay in the header.
 */
export function Plot({
  title,
  hint,
  actions,
  children,
}: {
  title: string;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="min-w-0">
      <header className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-xxs font-semibold uppercase tracking-wider text-muted-foreground">
            {title}
          </h3>
          {hint && (
            <p className="mt-0.5 text-xxs leading-snug text-muted-foreground/80">
              {hint}
            </p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/**
 * Sections inside one card, separated by a rule rather than by air.
 *
 * A card used to hold its plots in a `gap-4` grid, and the complaint that came
 * back was that the items inside a card did not read as separate items — which is
 * exactly right: whitespace alone cannot say "this figure belongs to that
 * heading" once the card is tall enough to hold four headings.
 *
 * `PlotStack` is the outer wrapper and the only piece that knows about the card's
 * padding: `-m-4` cancels it so the rules reach the card's edges, which is what
 * makes them read as divisions of the card rather than as boxes drawn inside it.
 * Every widget's content starts with one, and each child is one ruled band.
 */
export function PlotStack({ children }: { children: ReactNode }) {
  return <div className="-m-4 divide-y divide-border">{children}</div>;
}

/**
 * Sections side by side inside one band, ruled between.
 *
 * The rules are a 1px gap letting the border colour through from behind, not
 * `divide-x`. Tailwind's divide utilities walk flow order, so in a two-column grid
 * of four they give the third cell a *left* rule when what it needs is a top one —
 * correct for a row, wrong for the 2×2 the composition widget draws.
 *
 * The breakpoint is a container query, not a viewport one. These grids live inside
 * a widget the reader can drag down to half width, and `lg:grid-cols-2` there gave
 * two 200px plots on a wide screen — the viewport was wide, the card was not.
 * `WidgetGrid` puts `@container` on the card body; 48rem is the width below which
 * two plots stop being worth having.
 */
export function PlotGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-px bg-border @3xl:grid-cols-2">
      {children}
    </div>
  );
}

/**
 * One band of a `PlotStack` / cell of a `PlotGrid`. Carries the padding.
 *
 * `bg-card` is what makes `PlotGrid`'s gap read as a hairline rather than as a
 * stripe: the cell covers the grid's border-coloured background everywhere except
 * the gap.
 */
export function PlotCell({ children }: { children: ReactNode }) {
  return <div className="min-w-0 bg-card p-4">{children}</div>;
}

/**
 * A `PlotCell` band that folds to its heading.
 *
 * The agent drill-down stacks seven bands. Open, they ran to several screens on
 * every click of the leaderboard, and the two metered admin tools — quality
 * scores and failure triage — were the tallest and the least often read. So
 * every band folds, and every one starts folded: the heading row is the index,
 * and the reader opens what they came for.
 *
 * Folded means *unmounted*, not hidden. The quality and triage panels fire their
 * reads on mount, so a hidden-but-mounted panel would still spend three requests
 * per agent click for a section nobody opened; and a recharts surface measured
 * inside a `display: none` box comes back at 0×0 and stays there. The trade is
 * that a band forgets its own state (an opened thread row, a running poll) when
 * it folds — acceptable, because reopening re-reads the same server state.
 *
 * `summary` is the one figure the folded row still states ("턴 37", "모델 2개"), so
 * a closed band is a fact rather than a blank label. The hint is shown only
 * while open: the hints are two or three lines each, and eight of them on a
 * column of folded rows would be the wall of text folding was meant to remove.
 *
 * `flush` is for a body that is itself a `PlotGrid` of ruled cells (the trend
 * grid): it pulls the body out to the band's edges so those cells' own padding
 * and rules do the work, instead of padding inside padding.
 */
export function FoldBand({
  title,
  hint,
  summary,
  defaultOpen = true,
  flush = false,
  children,
}: {
  title: string;
  hint?: ReactNode;
  summary?: ReactNode;
  defaultOpen?: boolean;
  flush?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <PlotCell>
      <section className="min-w-0">
        <header className="flex items-start justify-between gap-2">
          {/* The whole heading is the control, not a chevron beside it: a 12px
              icon is a poor target and the label is what the eye lands on. */}
          <button
            type="button"
            className="-m-1 flex min-w-0 flex-1 items-start gap-1.5 rounded-sm p-1 text-left hover:bg-muted/40"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
          >
            <Chevron className="mt-px size-3 shrink-0 text-muted-foreground" />
            <span className="min-w-0">
              <span className="block text-xxs font-semibold uppercase tracking-wider text-muted-foreground">
                {title}
              </span>
              {open && hint && (
                <span className="mt-0.5 block text-xxs leading-snug text-muted-foreground/80">
                  {hint}
                </span>
              )}
            </span>
          </button>
          {summary && (
            <span className="shrink-0 text-xxs tabular-nums text-muted-foreground">
              {summary}
            </span>
          )}
        </header>
        {open &&
          (flush ? (
            <div className="-mx-4 -mb-4 mt-3 border-t border-border">{children}</div>
          ) : (
            <div className="mt-2">{children}</div>
          ))}
      </section>
    </PlotCell>
  );
}
