"use client";

import { costComposition, formatMicros } from "@/app/insights/insightsFormat.mjs";
import type { CostBlock } from "@/lib/insights";
import { EmptyPlot, Plot, PlotCell, ShareBar } from "./charts";

type Row = { key: string; label: string; micros: number; share: number };

/**
 * Where the money goes, one slice per component.
 *
 * Runtime is split into CPU and memory on purpose: on this platform the memory
 * half — billed for every second a session stays alive, idle or not — is most
 * of the bill (measured: ~60 GB-hours a day for one keep-warm session against
 * a third of a vCPU-hour). A stacked bar that showed "Runtime" as one slice hid
 * the one lever a governance reader actually has, which is the idle timeout.
 */
export function CostCompositionPanel({ cost }: { cost: CostBlock }) {
  const rows = costComposition(cost) as Row[];
  if (rows.length === 0) {
    return (
      <PlotCell>
        <EmptyPlot label="이 기간에 확정된 비용이 없습니다." />
      </PlotCell>
    );
  }
  const segments = rows.map((row) => ({
    name: row.label,
    value: row.micros / 1_000_000,
    display: formatMicros(row.micros),
  }));

  return (
    <PlotCell>
      <Plot
        title="비용 구성"
        hint="이 기간의 모델·Runtime·Gateway·Memory 비용을 한 줄로 놓았습니다."
      >
        <div className="space-y-3">
          <ShareBar segments={segments} />
          <table className="w-full text-xs">
            <tbody>
              {rows.map((row) => (
                <tr key={row.key} className="border-b border-border/50 last:border-0">
                  <td className="py-1.5 pr-3">{row.label}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{formatMicros(row.micros)}</td>
                  <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {(row.share * 100).toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {cost.memory.billed_micros !== null && cost.memory.micros === null && (
            <p className="text-xxs text-muted-foreground">
              Memory 저장분은 CloudWatch 지표가 없어 AWS 청구서 금액(24~48시간 지연)으로 보입니다.
            </p>
          )}
        </div>
      </Plot>
    </PlotCell>
  );
}
