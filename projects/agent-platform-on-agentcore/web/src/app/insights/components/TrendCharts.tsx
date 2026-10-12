"use client";

import { METRIC_COLOR } from "@/app/components/chartTheme";
import { dailySeries } from "@/app/insights/insightsFormat.mjs";
import type { DailyPoint } from "@/lib/insights";
import {
  DailyColumns,
  OverlaidDailyLines,
  Plot,
  PlotCell,
  PlotGrid,
  StackedDailyArea,
} from "./charts";

/**
 * The window, day by day: how much was asked, and what it cost in tokens.
 *
 * Two plots rather than one, and never a second y-axis. Turns are counts in the
 * hundreds and tokens are in the millions, so one plot would mean choosing an
 * alignment between two scales — and that choice invents a correlation the data
 * does not contain.
 *
 * Turns are columns because each day is a discrete count. Tokens are a stacked
 * area because input and output genuinely sum to the day's total, and both series
 * break on a day that was never measured: drawing zero there would assert we
 * measured nothing spent (`dailySeries`).
 *
 * The cache tiers are **out of the token stack**. In a stacked area the top band's
 * upper edge traces the running total, so a cache-read band sitting on top drew a
 * silhouette identical to input+output+cache — and since a cached read is a tenth
 * of an input token's price and typically dwarfs the fresh tokens on a caching
 * workload, the whole plot read as "this is the total". Split out, the token plot's
 * total is honestly input+output.
 *
 * Read and write share **one** plot, overlaid on a common axis rather than stacked:
 * they are different events at different rates (a read is ~1/10 an input token, a
 * write ~1.25×) so a sum would be meaningless, but the comparison is the whole point
 * — reads towering over writes is the cache paying off, and that only reads on a
 * shared axis (`OverlaidDailyLines`). Two separate auto-scaled plots would draw a
 * small write and a large read at the same height and hide the ratio.
 *
 * That leaves a second row of one, so it is paired with daily tool calls to keep the
 * grid a balanced 2×2. Both appear only when the window carries cache activity — in
 * practice any window with measured turns, since caching is on — so the grid is
 * either two plots (turns, tokens) or four, never an odd three with an empty cell.
 *
 * Returns one band, not a `PlotStack`: this renders both as a widget of its own and
 * inside the leaderboard drill-down's stack, and a stack nested in a stack would
 * apply the card-padding cancellation twice.
 */
export function TrendCharts({
  daily,
  partialDay,
}: {
  daily: DailyPoint[];
  /** The date still being written. Its column is a partial count, not a drop. */
  partialDay?: string | null;
}) {
  if (daily.length === 0) return null;
  const series = dailySeries(daily, partialDay) as Array<Record<string, unknown>>;
  const hasTokens = daily.some((point) => point.tokens_known);
  // Any cache activity — read or write — brings up the whole cache row, so the two
  // plots appear and vanish together and the grid stays balanced. A `null`
  // (unmeasured) day is not activity; only a measured day with a nonzero tier is.
  const hasCache = daily.some(
    (point) =>
      point.tokens_known &&
      ((point.cache_read_tokens ?? 0) > 0 || (point.cache_write_tokens ?? 0) > 0),
  );

  return (
    <PlotGrid>
      <PlotCell>
        <Plot title="일별 턴" hint="하루에 처리한 턴 수입니다.">
          <DailyColumns data={series} dataKey="turns" name="턴" colorIndex={METRIC_COLOR.turns} />
        </Plot>
      </PlotCell>
      <PlotCell>
        <Plot
          title="일별 토큰"
          hint={hasTokens ? "하루에 쓴 입력·출력 토큰입니다." : undefined}
        >
          {hasTokens ? (
            <StackedDailyArea
              data={series}
              series={[
                { key: "input_tokens", name: "입력", colorIndex: METRIC_COLOR.input_tokens },
                { key: "output_tokens", name: "출력", colorIndex: METRIC_COLOR.output_tokens },
              ]}
            />
          ) : (
            <p className="py-6 text-center text-xs text-muted-foreground">
              이 기간에는 토큰이 기록된 날이 없습니다.
            </p>
          )}
        </Plot>
      </PlotCell>
      {/* Second row, only when the window cached anything. Cache read vs write on
          one shared axis, then daily tool calls as the balancing fourth cell so the
          grid is 2 or 4, never a lopsided 3. Colours come from METRIC_COLOR, so each
          metric wears the same hue here as in its KPI sparkline. */}
      {hasCache && (
        <>
          <PlotCell>
            <Plot
              title="일별 캐시 읽기·쓰기"
              hint="캐시에서 되읽은 토큰과 새로 저장한 토큰입니다."
            >
              <OverlaidDailyLines
                data={series}
                series={[
                  { key: "cache_read_tokens", name: "캐시 읽기", colorIndex: METRIC_COLOR.cache_read_tokens },
                  { key: "cache_write_tokens", name: "캐시 쓰기", colorIndex: METRIC_COLOR.cache_write_tokens },
                ]}
              />
            </Plot>
          </PlotCell>
          <PlotCell>
            <Plot title="일별 툴 호출" hint="하루에 호출한 툴 횟수입니다.">
              <DailyColumns
                data={series}
                dataKey="tool_calls"
                name="툴 호출"
                colorIndex={METRIC_COLOR.tool_calls}
              />
            </Plot>
          </PlotCell>
        </>
      )}
    </PlotGrid>
  );
}
