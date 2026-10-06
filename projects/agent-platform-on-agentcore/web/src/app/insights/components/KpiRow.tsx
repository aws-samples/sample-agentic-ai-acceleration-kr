"use client";

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { METRIC_COLOR } from "@/app/components/chartTheme";
import {
  cacheHitRate,
  coverageHint,
  dailySeries,
  formatDelta,
  formatMicros,
  formatRate,
  formatTokens,
  periodDelta,
  rateOf,
} from "@/app/insights/insightsFormat.mjs";
import type { InsightsSummary } from "@/lib/insights";
import { Sparkline } from "./charts";

/**
 * One figure per tile, and none of them is an estimate.
 *
 * Every number here was written before the page loaded: turn counts and tokens
 * by the chat stream as each turn ended, model cost from the repository rate
 * card at that same moment. Runtime/Gateway/Memory are folded into the 총비용
 * headline only; their breakdown and the bill comparison live in their own
 * widgets (비용 구성, AWS 청구서 대조), so this row stays about what the chat did.
 *
 * The tiles used to carry a source badge each ("원장" / "계측" / "청구"). An admin
 * reading this page does not need to know which system produced a figure — only
 * that it is exact — so the badges are gone and a figure that cannot be stated
 * is a dash with its reason in the caption, never a number with a caveat. A
 * complete figure carries no caption at all.
 */

type PeriodDelta = { pct: number; half: number; earlier: number; later: number } | null;

function Delta({ delta }: { delta: PeriodDelta }) {
  const pct = delta ? Math.round(delta.pct) : 0;
  const Icon = !delta || pct === 0 ? Minus : pct > 0 ? ArrowUpRight : ArrowDownRight;

  return (
    <span className="inline-flex items-center gap-1 text-xxs text-muted-foreground">
      <Icon className="size-3 shrink-0" />
      <span className="tabular-nums">{formatDelta(delta)}</span>
      {delta && <span className="opacity-70">· 앞 {delta.half}일 대비</span>}
    </span>
  );
}

function Tile({
  label,
  value,
  hint,
  spark,
  delta,
}: {
  label: string;
  value: string;
  hint?: string;
  spark?: {
    data: Array<Record<string, unknown>>;
    dataKey: string;
    colorIndex: number;
  };
  delta?: PeriodDelta;
}) {
  return (
    <div className="flex h-full flex-col justify-between rounded-md border border-border p-3">
      <div>
        <div className="mb-1 text-xs text-muted-foreground">{label}</div>
        <div className="text-lg font-semibold">{value}</div>
        {delta !== undefined && (
          <div className="mt-0.5">
            <Delta delta={delta} />
          </div>
        )}
        {hint && <div className="mt-1 text-xxs text-muted-foreground">{hint}</div>}
      </div>
      {spark && (
        <div className="mt-2">
          <Sparkline data={spark.data} dataKey={spark.dataKey} name={label} colorIndex={spark.colorIndex} />
        </div>
      )}
    </div>
  );
}

export function KpiRow({ summary }: { summary: InsightsSummary }) {
  const { totals, cost } = summary;
  const series = dailySeries(summary.daily, summary.partial_day) as Array<Record<string, unknown>>;
  const partial = summary.partial_day;
  const turnDelta = periodDelta(summary.daily, "turns", partial) as PeriodDelta;
  const inputDelta = periodDelta(summary.daily, "input_tokens", partial) as PeriodDelta;
  const outputDelta = periodDelta(summary.daily, "output_tokens", partial) as PeriodDelta;

  const failure = rateOf(totals.failed_turns, totals.turns) as { rate: number } | null;
  const cache = cacheHitRate(totals) as { rate: number } | null;

  // The caption says exactly which turns the figure leaves out, or nothing
  // when the window is complete. `unpriced_turns` on the cost block is the
  // same counter the totals carry; the totals also know the turns whose tokens
  // arrived without a price and the turns that never reported tokens.
  const modelHint = (coverageHint(totals) as string | null) ?? undefined;
  const tokenHint =
    totals.unmeasured_turns > 0
      ? `토큰 기록 없음 ${totals.unmeasured_turns.toLocaleString("ko-KR")}턴 제외`
      : undefined;

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/30 p-3 @md:flex-row @md:items-center">
        <div className="min-w-0 shrink-0">
          <div className="text-xs text-muted-foreground">턴</div>
          <div className="text-4xl font-semibold leading-tight">
            {totals.turns.toLocaleString("ko-KR")}
          </div>
          <Delta delta={turnDelta} />
        </div>
        <div className="min-w-0 flex-1 @md:max-w-md">
          <Sparkline data={series} dataKey="turns" name="턴" colorIndex={METRIC_COLOR.turns} />
        </div>
        {/* The headline dollar figure sits with the headline count: total known
            cost across every component. Absent — not $0 — until both the model
            and the runtime figure exist for this window. */}
        <div className="min-w-0 shrink-0 @md:border-l @md:border-border @md:pl-4">
          <div className="text-xs text-muted-foreground">총비용</div>
          <div className="text-3xl font-semibold leading-tight">
            {formatMicros(cost.total_micros)}
          </div>
          <div className="text-xxs text-muted-foreground">
            {cost.total_micros === null
              ? "모델 비용과 Runtime 비용이 모두 집계되면 표시됩니다"
              : "모델 + Runtime + Gateway + Memory"}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 @3xl:grid-cols-[minmax(0,6fr)_minmax(0,1fr)]">
        <section className="flex min-w-0 flex-col">
          <h4 className="mb-1.5 text-xxs font-semibold uppercase tracking-wider text-muted-foreground">
            사용량
          </h4>
          <div className="grid flex-1 grid-cols-2 gap-2 @xl:grid-cols-3 @3xl:grid-cols-6">
            <Tile
              label="입력 토큰"
              value={formatTokens(totals.input_tokens, totals.unmeasured_turns)}
              delta={inputDelta}
              hint={tokenHint}
              spark={{ data: series, dataKey: "input_tokens", colorIndex: METRIC_COLOR.input_tokens }}
            />
            <Tile
              label="출력 토큰"
              value={formatTokens(totals.output_tokens, totals.unmeasured_turns)}
              delta={outputDelta}
              hint={tokenHint}
              spark={{ data: series, dataKey: "output_tokens", colorIndex: METRIC_COLOR.output_tokens }}
            />
            <Tile
              label="캐시 적중"
              value={formatRate(cache)}
              hint={cache ? "입력 토큰 중 캐시에서 읽은 비율" : "토큰이 기록된 턴이 없습니다"}
            />
            <Tile
              label="툴 호출"
              value={totals.tool_calls.toLocaleString("ko-KR")}
              spark={{ data: series, dataKey: "tool_calls", colorIndex: METRIC_COLOR.tool_calls }}
            />
            <Tile
              label="실패한 턴"
              value={totals.failed_turns.toLocaleString("ko-KR")}
              hint={
                failure
                  ? `전체 턴의 ${formatRate(failure)} · 모델·런타임 오류`
                  : "모델·런타임 오류로 끝난 턴"
              }
              spark={{ data: series, dataKey: "failed_turns", colorIndex: METRIC_COLOR.failed_turns }}
            />
            <Tile
              label="중단된 턴"
              value={totals.interrupted_turns.toLocaleString("ko-KR")}
              hint="클라이언트 연결 끊김"
              spark={{ data: series, dataKey: "interrupted_turns", colorIndex: METRIC_COLOR.interrupted_turns }}
            />
          </div>
        </section>
        <section className="flex min-w-0 flex-col">
          <h4 className="mb-1.5 text-xxs font-semibold uppercase tracking-wider text-muted-foreground">
            비용
          </h4>
          <div className="grid flex-1 grid-cols-1 gap-2">
            {/* Priced when each turn ended, by the model it ran on, at the
                repository rate card. The caption lists every turn the figure
                leaves out, so the number is never read as the whole window when
                it is not. */}
            <Tile
              label="모델 비용"
              value={formatMicros(cost.model.micros, {
                unpriced: cost.model.unpriced_turns,
                unmeasured: totals.unmeasured_turns,
              })}
              hint={modelHint}
            />
          </div>
        </section>
      </div>
    </div>
  );
}
