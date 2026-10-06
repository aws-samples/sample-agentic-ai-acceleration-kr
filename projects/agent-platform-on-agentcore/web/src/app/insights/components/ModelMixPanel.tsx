"use client";

import { useState } from "react";

import { METRIC_COLOR, COST_COLOR } from "@/app/components/chartTheme";
import { Button } from "@/components/ui/button";
import { formatMicros, formatTokens, topNWithOther } from "@/app/insights/insightsFormat.mjs";
import type { ModelRow } from "@/lib/insights";
import { EmptyPlot, Plot, PlotCell, PlotGrid, RankedBars, type RankedEntry } from "./charts";

/**
 * Which models the fleet actually ran, by turns and by cost.
 *
 * Read from the per-(agent, model) counters the ledger writes at turn time, so
 * a window spanning a redeploy shows both models with their own tokens — the
 * old read-time lookup showed one model per agent and re-priced history under
 * it. A model the rate card does not know is listed with "요율 미등록", never
 * dropped: the turns happened and the reader should see that they are unpriced.
 */
export function ModelMixPanel({ models }: { models: ModelRow[] }) {
  const [by, setBy] = useState<"turns" | "cost">("turns");
  if (models.length === 0) {
    return (
      <PlotCell>
        <EmptyPlot label="이 기간에 모델별로 기록된 턴이 없습니다." />
      </PlotCell>
    );
  }

  const byTurns = topNWithOther(
    models.map((row) => ({ name: row.model_id, value: row.turns })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: `${entry.value.toLocaleString("ko-KR")}턴`,
  })) as RankedEntry[];

  const priced = models.filter((row) => row.model_cost_micros !== null);
  const byCost = topNWithOther(
    priced.map((row) => ({ name: row.model_id, value: (row.model_cost_micros ?? 0) / 1_000_000 })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: `$${entry.value.toFixed(4)}`,
  })) as RankedEntry[];

  const unregistered = models.filter((row) => !row.registered);

  return (
    <>
      <PlotGrid>
        <PlotCell>
          <Plot
            title={by === "turns" ? "모델별 턴" : "모델별 비용"}
            hint="턴이 끝난 시점에 실제로 호출된 모델 기준입니다."
            actions={
              <div className="flex gap-1">
                <Button size="sm" variant={by === "turns" ? "default" : "ghost"} onClick={() => setBy("turns")}>
                  턴
                </Button>
                <Button size="sm" variant={by === "cost" ? "default" : "ghost"} onClick={() => setBy("cost")}>
                  비용
                </Button>
              </div>
            }
          >
            {by === "turns" ? (
              <RankedBars entries={byTurns} colorIndex={METRIC_COLOR.turns} />
            ) : byCost.length > 0 ? (
              <RankedBars entries={byCost} color={COST_COLOR} />
            ) : (
              <EmptyPlot label="요율표에 등록된 모델의 턴이 없습니다." />
            )}
          </Plot>
        </PlotCell>
        <PlotCell>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="whitespace-nowrap px-3 py-2 text-left font-medium">모델</th>
                  <th className="whitespace-nowrap px-3 py-2 text-left font-medium">라우팅</th>
                  <th className="whitespace-nowrap px-3 py-2 text-right font-medium">턴</th>
                  <th className="whitespace-nowrap px-3 py-2 text-right font-medium">입력</th>
                  <th className="whitespace-nowrap px-3 py-2 text-right font-medium">출력</th>
                  <th className="whitespace-nowrap px-3 py-2 text-right font-medium">비용</th>
                </tr>
              </thead>
              <tbody>
                {models.map((row) => (
                  <tr key={row.model_id} className="border-b border-border/50 last:border-0">
                    <td className="max-w-64 truncate px-3 py-2 font-mono text-xxs" title={row.model_id}>
                      {row.model_id}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                      {row.routing === "global" ? "global" : row.routing === "regional" ? "regional" : "—"}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {row.turns.toLocaleString("ko-KR")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatTokens(row.input_tokens)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatTokens(row.output_tokens)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatMicros(row.model_cost_micros, { unpriced: row.registered ? 0 : row.turns })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {unregistered.length > 0 && (
            <p className="mt-2 text-xxs text-warning">
              요율표에 없는 모델 {unregistered.length}개: {unregistered.map((row) => row.model_id).join(", ")}. AWS 청구서에
              해당 모델이 이틀 연속 잡히면 요율이 자동 등록되고, Settings 의 모델 요율에서 지금 등록해도 비용이 계산됩니다.
            </p>
          )}
        </PlotCell>
      </PlotGrid>
    </>
  );
}
