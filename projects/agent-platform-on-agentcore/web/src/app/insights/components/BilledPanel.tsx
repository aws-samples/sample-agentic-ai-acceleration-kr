"use client";

import { COST_COLOR } from "@/app/components/chartTheme";
import type { AgentUsageRow, CostBlock } from "@/lib/insights";
import {
  COMPONENT_LABELS,
  billedGap,
  formatMicros,
  overlaySummary,
} from "@/app/insights/insightsFormat.mjs";
import { Dumbbell, EmptyPlot, Plot, PlotCell, PlotGrid, RankedBars, type RankedEntry } from "./charts";

const LABELS = COMPONENT_LABELS as Record<string, string>;

/** A single figure with its caption. */
function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
      {hint && <p className="mt-1 text-xxs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/**
 * The AWS bill against this page's figures — the check, never the source.
 *
 * Three comparisons, all written by the six-hourly reconciliation into DDB and
 * read here without a Cost Explorer call:
 *
 * - **per agent**: billed runtime (AgentName tag) against this page's Runtime
 *   figure (CloudWatch quantities at the published rate), summed over the days
 *   the bill has reached;
 * - **per component**: the latest day's billed split against this page's slices;
 * - **the rate card**: every (model, routing, tier) rate the account was
 *   actually charged against the table the ledger priced with. A mismatch is a
 *   bug in the table and is shown as one.
 *
 * Cost Explorer lags 24–48 hours, so the newest days have no billed figure;
 * `latest_day` says how far the check has reached and today is never in it.
 *
 * Vocabulary, fixed on purpose: "AWS 청구서" is Cost Explorer, "이 페이지" is what
 * the rest of the dashboard shows. The earlier "청구 / 계측 / 우리 / 차이" named
 * the systems instead of the comparison, and read as doubt about the numbers.
 */
export function BilledPanel({ cost, agents }: { cost: CostBlock; agents: AgentUsageRow[] }) {
  const billed = cost.billed;
  const rate = cost.rate_card;
  const learned = rate.learned ?? [];

  const compared = agents.filter((row) => row.billed_runtime_micros !== null);
  const perAgent: RankedEntry[] = compared
    .map((row) => ({
      name: row.name,
      value: (row.billed_runtime_micros ?? 0) / 1_000_000,
      display: formatMicros(row.billed_runtime_micros),
      meta:
        row.cost_diff_micros !== null
          ? `이 페이지 ${formatMicros((row.billed_runtime_micros ?? 0) - row.cost_diff_micros)} · ${billedGap({
              runtime_micros: row.cost_diff_micros,
              runtime_pct:
                row.billed_runtime_micros && row.billed_runtime_micros !== row.cost_diff_micros
                  ? row.cost_diff_micros / (row.billed_runtime_micros - row.cost_diff_micros)
                  : null,
            })}`
          : "이 페이지에 같은 날의 Runtime 비용이 없습니다",
    }))
    .sort((a, b) => b.value - a.value);

  const components = billed
    ? Object.entries(billed.component)
        .map(([key, micros]) => ({
          key,
          label: LABELS[key] ?? key,
          billed: micros,
          ours:
            key === "runtime"
              ? cost.runtime.micros
              : key === "gateway"
                ? cost.gateway.micros
                : key === "memory"
                  ? cost.memory.micros
                  : null,
        }))
        .sort((a, b) => b.billed - a.billed)
    : [];

  return (
    <>
      <PlotCell>
        <div className="grid grid-cols-1 gap-2 @xl:grid-cols-3">
          <Figure
            label="AWS 청구서의 Runtime 비용"
            value={billed ? formatMicros(billed.runtime_micros) : "집계 대기"}
            hint={
              billed
                ? `이 플랫폼 태그 기준 · ${billed.latest_day ?? "?"} 까지 반영${billed.ce_estimated ? " · 최근 일자는 잠정치" : ""}`
                : "청구서가 태그를 반영하기까지 24~48시간이 걸립니다"
            }
          />
          <Figure
            label="이 페이지의 Runtime 비용과 비교"
            value={billedGap(cost.diff)}
            hint={
              cost.diff
                ? `청구서에 잡힌 완결된 ${cost.diff.days_compared}일만 같은 날끼리 비교${cost.diff.through ? ` (${cost.diff.through} 까지)` : ""}. 진행 중인 오늘은 넣지 않고, 0.1% 미만은 청구서 반올림 범위라 일치로 봅니다.`
                : "비교할 청구 일자가 아직 없습니다"
            }
          />
          <Figure
            label={`모델 요율표 ${rate.version}`}
            value={
              rate.mismatches.length === 0 && rate.unregistered_families.length === 0
                ? "청구서 요율과 일치"
                : rate.mismatches.length > 0
                  ? `불일치 ${rate.mismatches.length}건`
                  : `미등록 ${rate.unregistered_families.length}개`
            }
            hint={[
              rate.checked_at
                ? `${rate.checked_at.slice(0, 16).replace("T", " ")} 에 AWS 청구서의 일별 요율과 비교`
                : "아직 청구서와 비교하지 않았습니다",
              overlaySummary(learned),
              rate.mismatches.length > 0 || rate.unregistered_families.length > 0
                ? "같은 값이 이틀 연속 청구되면 자동 반영되고, Settings 의 모델 요율에서 바로 등록할 수도 있습니다"
                : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          />
        </div>
      </PlotCell>

      {(rate.mismatches.length > 0 || rate.unregistered_families.length > 0) && (
        <PlotCell>
          <Plot title="요율표 점검" hint="AWS 청구서의 가장 최근 청구일 요율과 그날 유효한 요율표가 다른 항목입니다. 같은 값이 이틀 연속 청구되면 그 날짜부터의 요율로 자동 등록되고 해당 턴이 다시 계산됩니다.">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-2 py-1 text-left font-medium">모델</th>
                  <th className="px-2 py-1 text-left font-medium">라우팅</th>
                  <th className="px-2 py-1 text-left font-medium">tier</th>
                  <th className="px-2 py-1 text-right font-medium">요율표 $/1M</th>
                  <th className="px-2 py-1 text-right font-medium">청구서 $/1M</th>
                </tr>
              </thead>
              <tbody>
                {rate.mismatches.map((row) => (
                  <tr key={`${row.family}|${row.routing}|${row.tier}`} className="border-b border-border/50 text-destructive last:border-0">
                    <td className="px-2 py-1 font-mono text-xxs">{row.family}</td>
                    <td className="px-2 py-1">{row.routing}</td>
                    <td className="px-2 py-1">{row.tier}</td>
                    <td className="px-2 py-1 text-right tabular-nums">{formatMicros(row.card_micro)}</td>
                    <td className="px-2 py-1 text-right tabular-nums">{formatMicros(row.billed_micro)}</td>
                  </tr>
                ))}
                {rate.unregistered_families.map((family) => (
                  <tr key={family} className="border-b border-border/50 text-warning last:border-0">
                    <td className="px-2 py-1 font-mono text-xxs">{family}</td>
                    <td className="px-2 py-1" colSpan={2}>청구서에는 있고 요율표에는 없음 (이틀 연속 확인 후 자동 등록, 또는 Settings 의 모델 요율에서 등록)</td>
                    <td className="px-2 py-1 text-right">—</td>
                    <td className="px-2 py-1 text-right">청구서 참조</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Plot>
        </PlotCell>
      )}

      <PlotGrid>
        <PlotCell>
          <Plot
            title="에이전트별 AWS 청구서 Runtime 비용"
            hint="에이전트 태그 기준 청구 금액, 완결된 날만. 작은 글씨는 같은 날들의 이 페이지 Runtime 비용과의 비교입니다."
          >
            {perAgent.length > 0 ? (
              <RankedBars entries={perAgent} color={COST_COLOR} />
            ) : (
              <EmptyPlot label="청구서가 아직 이 기간의 에이전트 태그에 닿지 않았습니다." />
            )}
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot
            title="구성별 비교"
            hint="AWS 청구서(채운 점)와 이 페이지(빈 점)입니다. Memory 저장분처럼 CloudWatch 지표가 없는 항목은 청구서만 보입니다."
          >
            {components.length > 0 ? (
              <div className="space-y-3">
                {components.map((row) => (
                  <div key={row.key}>
                    <div className="mb-1 text-xxs text-muted-foreground">{row.label}</div>
                    {row.ours !== null ? (
                      <Dumbbell
                        reference={{ name: "이 페이지", value: row.ours / 1_000_000 }}
                        actual={{ name: "AWS 청구서", value: row.billed / 1_000_000 }}
                        format={(value: number) => `$${value.toFixed(4)}`}
                      />
                    ) : (
                      <div className="text-xs tabular-nums">
                        AWS 청구서 {formatMicros(row.billed)} <span className="text-muted-foreground">· 이 페이지에는 지표 없음</span>
                      </div>
                    )}
                  </div>
                ))}
                <p className="text-xxs text-muted-foreground">
                  청구서 쪽은 완결된 마지막 날의 스냅샷이고 이 페이지 쪽은 기간 전체라, 최근 1~2일만큼 이 페이지가 더 큽니다.
                  Gateway 는 청구서가 세는 툴 요청(tools/list·tools/call)만 값을 매기고, MCP 핸드셰이크 요청은 호출 수에만 들어 있습니다.
                </p>
              </div>
            ) : (
              <EmptyPlot label="구성별 청구 스냅샷이 아직 없습니다." />
            )}
          </Plot>
        </PlotCell>
      </PlotGrid>
    </>
  );
}
