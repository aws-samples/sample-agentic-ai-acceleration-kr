"use client";

import { COST_COLOR, METRIC_COLOR, MUTED_MARK, SERIES_COLORS } from "@/app/components/chartTheme";
import { formatMicros, formatRate, formatTokens } from "@/app/insights/insightsFormat.mjs";
import {
  alertSeries,
  perMember,
  teamDailyRows,
  teamHeadline,
  teamSeries,
  UNATTRIBUTED_KEY,
} from "@/app/insights/teamInsights.mjs";
import { budgetLabel, budgetTone, formatBudgetCell } from "@/app/org/teamBudget.mjs";
import { ToneBadge } from "@/app/org/components/ToneBadge";
import type { TeamInsights } from "@/lib/insights";
import {
  DonutShare,
  EmptyPlot,
  FoldBand,
  Plot,
  PlotCell,
  PlotGrid,
  PlotStack,
  RankedBars,
  StackedDailyColumns,
  ThresholdColumns,
  type RankedEntry,
} from "./charts";
import { KpiTile } from "./KpiTile";
import { useTeamInsights, type TeamInsightsState } from "./useTeamInsights";

const usd = (v: number) => `$${v.toFixed(v >= 1 ? 2 : 4)}`;

/**
 * Spend per team — the chargeback view an organisation owner asks for: who
 * spends, how it moves day to day, how hard each team uses the platform per
 * head, and which teams crossed the daily alert they were given.
 *
 * Rows come from the ledger (turns attributed by the caller's groups at the
 * time they ran). Turns by admins and teamless people are the unattributed
 * remainder, drawn in the muted mark beside the teams and never spread across
 * them. Gateway policy lives in its own widget (PolicyPanel), off the same read.
 */
export function TeamPanel({ days }: { days: number }) {
  const result = useTeamInsights(days);
  if (result.state !== "ready") return <TeamInsightsPending result={result} />;
  const data = result.data;
  if (data.teams.length === 0) {
    return <Notice label="이 배포에는 팀이 없습니다. 조직 관리(/org)에서 팀을 만드세요." />;
  }
  return <TeamUsage data={data} />;
}

/** A one-line widget body, in the same band chrome as the charts. */
export function Notice({ label }: { label: string }) {
  return (
    <PlotStack>
      <PlotCell>
        <EmptyPlot label={label} />
      </PlotCell>
    </PlotStack>
  );
}

/** The non-ready states both team widgets render the same way. */
export function TeamInsightsPending({ result }: { result: Exclude<TeamInsightsState, { state: "ready" }> }) {
  if (result.state === "forbidden") return <Notice label="관리자만 볼 수 있습니다." />;
  if (result.state === "error") return <Notice label={`불러오지 못했습니다: ${result.message}`} />;
  return <Notice label="불러오는 중…" />;
}

function TeamUsage({ data }: { data: TeamInsights }) {
  const partialDay = data.partial_day ?? null;
  const series = teamSeries(data.teams);
  const stacked = [
    ...series,
    { key: UNATTRIBUTED_KEY, name: "미귀속", color: MUTED_MARK },
  ];
  const costRows = teamDailyRows(data.teams, data.unattributed, "model_cost_micros", partialDay);
  const turnRows = teamDailyRows(data.teams, data.unattributed, "turns", partialDay);
  const head = teamHeadline(data);
  const colorOf = new Map(series.flatMap((s) => s.members.map((m) => [m, s.colorIndex ?? 0] as const)));

  const costSegments = [
    ...series.map((s) => ({
      name: s.name,
      value: data.teams
        .filter((t) => s.members.includes(t.team))
        .reduce((n, t) => n + t.model_cost_micros, 0) / 1_000_000,
      colorIndex: s.colorIndex ?? 0,
    })),
  ];

  const perHeadTurns: RankedEntry[] = data.teams
    .map((t) => {
      const v = perMember(t.turns, t.members);
      return {
        name: t.label || t.team,
        value: v ?? 0,
        display: v === null ? "—" : `${v.toFixed(1)}턴`,
        meta: t.members === null ? "구성원 수 모름" : `${t.members}명 · 총 ${t.turns}턴`,
      };
    })
    .sort((a, b) => b.value - a.value);

  const perTurnCost: RankedEntry[] = data.teams
    .filter((t) => t.priced_turns > 0)
    .map((t) => {
      const v = t.model_cost_micros / t.priced_turns;
      const fail = t.turns > 0 ? t.failed_turns / t.turns : null;
      return {
        name: t.label || t.team,
        value: v,
        display: formatMicros(v),
        meta: fail === null ? undefined : `실패 ${formatRate({ rate: fail })}`,
      };
    })
    .sort((a, b) => b.value - a.value);

  return (
    <PlotStack>
      <PlotCell>
        <div className="grid grid-cols-2 gap-2 @3xl:grid-cols-4">
          <KpiTile
            label="팀 귀속 모델 비용"
            value={formatMicros(head.attributedCostMicros)}
            hint={
              head.attributedShare === null
                ? "이 기간 비용 없음"
                : `전체의 ${formatRate({ rate: head.attributedShare })} · 미귀속 ${formatMicros(data.unattributed.model_cost_micros)}`
            }
            spark={{
              data: costRows.map((r) => ({ ...r, total: series.reduce((n, s) => n + Number(r[s.key] || 0), 0) })),
              dataKey: "total",
              colorIndex: 0,
            }}
          />
          <KpiTile
            label="팀 턴"
            value={head.turns.toLocaleString("ko-KR")}
            hint={`미귀속 ${data.unattributed.turns.toLocaleString("ko-KR")}턴`}
            spark={{
              data: turnRows.map((r) => ({ ...r, total: series.reduce((n, s) => n + Number(r[s.key] || 0), 0) })),
              dataKey: "total",
              colorIndex: METRIC_COLOR.turns,
            }}
          />
          <KpiTile
            label="실패율"
            value={head.failureRate === null ? "—" : formatRate({ rate: head.failureRate })}
            hint="팀 턴 중 실패로 끝난 비율"
          />
          <KpiTile
            label="일일 경고 초과 팀"
            value={head.teamsWithAlert === 0 ? "—" : `${head.teamsOverAlert} / ${head.teamsWithAlert}`}
            hint={head.teamsWithAlert === 0 ? "경고를 설정한 팀이 없습니다" : "경고를 설정한 팀 중 이 기간 하루라도 넘은 팀"}
          />
        </div>
      </PlotCell>

      <PlotGrid>
        <PlotCell>
          <Plot
            title="일별 팀 모델 비용"
            hint="팀별로 쌓은 하루 비용입니다. 회색은 관리자·팀 없는 사용자의 미귀속 비용입니다. *는 집계 중인 오늘입니다."
          >
            <StackedDailyColumns data={costRows} series={stacked} format={usd} empty="이 기간 기록된 비용이 없습니다." />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="팀별 비용 점유" hint="이 기간 팀 귀속 모델 비용의 팀별 몫입니다.">
            <DonutShare segments={costSegments} format={usd} empty="이 기간 팀에 귀속된 비용이 없습니다." />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="1인당 턴" hint="팀 턴을 현재 구성원 수로 나눈 값입니다. 팀이 플랫폼을 얼마나 쓰는지 봅니다.">
            <RankedBars entries={perHeadTurns} colorIndex={METRIC_COLOR.turns} empty="턴이 기록된 팀이 없습니다." />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="턴당 모델 비용" hint="가격이 매겨진 턴만으로 나눈 값입니다. 옆의 숫자는 팀 턴 중 실패 비율입니다.">
            <RankedBars entries={perTurnCost} color={COST_COLOR} empty="가격이 매겨진 턴이 있는 팀이 없습니다." />
          </Plot>
        </PlotCell>
      </PlotGrid>

      <PlotCell>
        <Plot
          title="일일 경고 대비"
          hint="팀마다 하루 비용을 막대로, 일일 경고를 점선으로 그립니다. 경고에 닿은 날은 붉게 표시됩니다."
        >
          <div className="grid grid-cols-1 gap-3 @xl:grid-cols-2 @4xl:grid-cols-3">
            {data.teams.map((t) => {
              const budget = budgetTone(t.today_cost_micros, t.daily_cost_alert_usd);
              const word = budgetLabel(budget.tone);
              return (
                <div key={t.team} className="rounded-md border border-border p-3">
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <span className="flex min-w-0 items-center gap-1.5 font-medium">
                      <span
                        className="size-2 shrink-0 rounded-sm"
                        style={{ backgroundColor: SERIES_COLORS[colorOf.get(t.team) ?? 0] }}
                      />
                      <span className="truncate">{t.label || t.team}</span>
                    </span>
                    {word && <ToneBadge label={word} tone={budget.tone} />}
                  </div>
                  <div className="mt-0.5 text-xxs text-muted-foreground tabular-nums">
                    오늘 {formatBudgetCell(t.today_cost_micros, t.daily_cost_alert_usd)}
                    {t.days_over_alert !== null && t.days_over_alert > 0 && ` · 초과 ${t.days_over_alert}일`}
                    {t.daily_cost_alert_usd === null && " · 경고 없음"}
                  </div>
                  {t.max_day_cost_micros > 0 || t.daily_cost_alert_usd !== null ? (
                    <ThresholdColumns
                      data={alertSeries(t, partialDay)}
                      threshold={t.daily_cost_alert_usd}
                      colorIndex={colorOf.get(t.team) ?? 0}
                      format={usd}
                    />
                  ) : (
                    // Nothing to draw and no line to draw it against: say so at the plot's height.
                    <p className="flex h-20 items-center justify-center text-xxs text-muted-foreground">
                      이 기간 비용 없음
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </Plot>
      </PlotCell>

      <FoldBand title="팀별 상세" summary={`${data.teams.length}개 팀`} defaultOpen={false}>
        <div className="max-h-[26rem] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 z-[1] bg-card">
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="py-1 font-medium">팀</th>
                <th className="font-medium">구성원</th>
                <th className="font-medium">턴</th>
                <th className="font-medium">실패</th>
                <th className="font-medium">툴 호출</th>
                <th className="font-medium">토큰(입력/출력)</th>
                <th className="font-medium">모델 비용</th>
                <th className="font-medium">정책 거부</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {data.teams.map((r) => (
                <tr key={r.team} className="border-b border-border/50">
                  <td className="py-1 font-medium">{r.label || r.team}</td>
                  <td>{r.members ?? "—"}</td>
                  <td>{r.turns}</td>
                  <td>{r.failed_turns}</td>
                  <td>{r.tool_calls}</td>
                  <td>
                    {formatTokens(r.input_tokens)} / {formatTokens(r.output_tokens)}
                  </td>
                  <td>{r.priced_turns > 0 ? formatMicros(r.model_cost_micros) : "—"}</td>
                  <td>{r.policy_denials}</td>
                </tr>
              ))}
              <tr className="text-muted-foreground">
                <td className="py-1">미귀속</td>
                <td>—</td>
                <td>{data.unattributed.turns}</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
                <td>{formatMicros(data.unattributed.model_cost_micros)}</td>
                <td>{data.unattributed.policy_denials}</td>
              </tr>
            </tbody>
          </table>
          <p className="mt-2 text-xxs text-muted-foreground">
            오늘은 집계 중인 값이며, 가격이 아직 없는 턴은 다음날 반영됩니다. 구성원 수는 지금 기준입니다.
          </p>
        </div>
      </FoldBand>
    </PlotStack>
  );
}
