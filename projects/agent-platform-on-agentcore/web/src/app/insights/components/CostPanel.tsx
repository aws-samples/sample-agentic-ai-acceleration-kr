"use client";

import type { AgentUsageRow, CostBlock } from "@/lib/insights";
import { billedTrustNote, costBreakdown, formatMicros } from "@/app/insights/insightsFormat.mjs";
import { DonutShare, EmptyPlot, Plot, PlotCell, PlotGrid, TreemapShare, type TreemapEntry } from "./charts";

type Row = { key: string; label: string; micros: number; share: number; source: string; colorIndex: number };

const dollars = (value: number) => formatMicros(Math.round(value * 1_000_000));

/**
 * Where the window's money went: by component, then by agent.
 *
 * Until 2026-10-10 this was two widgets, 비용 구성 and AgentCore 청구 비용, and both
 * listed Runtime, Gateway and Memory — one from this page's meters, one from the
 * AWS bill, with figures that differed by the bill's 24–48 hour lag. A reader saw
 * the same component twice at two prices. Now each component is listed once
 * (`costBreakdown`): metered where the page meters it, so today is in it, and from
 * the bill only for what nothing else measures (Code Interpreter, Browser, …).
 *
 * Runtime stays split into CPU and memory on purpose: on this platform the memory
 * half — billed for every second a session stays alive, idle or not — is most of
 * the bill (measured: ~60 GB-hours a day for one keep-warm session against a third
 * of a vCPU-hour), and the idle timeout is the one lever a governance reader has.
 *
 * A donut for the components, because the question is share of the whole and
 * there are at most eight. A treemap for agents, because there can be many more
 * than eight and the question is still who the bulk of it is.
 */
export function CostPanel({ cost, agents }: { cost: CostBlock; agents: AgentUsageRow[] }) {
  const rows = costBreakdown(cost) as Row[];
  // The bill's check on the metered figures, as a footnote — once every complete
  // day agreed within 0.1%, one line is what the comparison is worth on screen.
  const trust = billedTrustNote(cost.diff);
  const fromBill = rows.filter((row) => row.source === "billed").map((row) => row.label);

  const perAgent: TreemapEntry[] = agents
    .filter((row) => (row.total_cost_micros ?? 0) > 0)
    .map((row) => ({
      name: row.name,
      value: (row.total_cost_micros ?? 0) / 1_000_000,
      display: formatMicros(row.total_cost_micros),
      id: row.record_id,
    }))
    .sort((a, b) => b.value - a.value);
  // Runtimes no registry record claims (deleted agents, harness companions) are in
  // the component total but on no agent row; left out, the tiles would sum to half
  // the donut beside them. Grey, because it is a remainder, not an agent.
  const unclaimed = cost.runtime.unclaimed_micros ?? 0;
  if (unclaimed > 0) {
    perAgent.push({
      name: `에이전트 미연결 Runtime ${cost.runtime.unclaimed_runtimes?.length ?? 0}개`,
      value: unclaimed / 1_000_000,
      display: formatMicros(unclaimed),
      muted: true,
    });
  }

  return (
    <PlotGrid>
      <PlotCell>
        <Plot title="구성별 비용" hint="모델 토큰과 AgentCore 각 기능에 든 비용입니다.">
          {rows.length > 0 ? (
            <div className="space-y-2">
              <DonutShare
                segments={rows.map((row) => ({
                  name: row.label,
                  value: row.micros / 1_000_000,
                  colorIndex: row.colorIndex,
                }))}
                format={dollars}
                empty="이 기간에 확정된 비용이 없습니다."
              />
              {fromBill.length > 0 && (
                <p className="text-xxs text-muted-foreground">
                  {fromBill.join(", ")}은(는) CloudWatch 지표가 없어 AWS 청구서 금액(24~48시간 지연)입니다.
                </p>
              )}
              {trust && <p className="text-xxs text-muted-foreground">{trust}</p>}
            </div>
          ) : (
            <EmptyPlot label="이 기간에 확정된 비용이 없습니다." />
          )}
        </Plot>
      </PlotCell>
      <PlotCell>
        <Plot title="에이전트별 비용" hint="모델과 Runtime 비용의 합입니다. 칸의 넓이가 금액이고, 회색은 어느 에이전트에도 연결되지 않은 Runtime 입니다.">
          <TreemapShare entries={perAgent} />
        </Plot>
      </PlotCell>
    </PlotGrid>
  );
}
