"use client";

import { METRIC_COLOR, MUTED_MARK, SERIES_COLORS } from "@/app/components/chartTheme";
import { formatRate, subjectLabel } from "@/app/insights/insightsFormat.mjs";
import {
  decisionRows,
  denialMatrix,
  teamDailyRows,
  teamSeries,
  toolLabel,
  UNATTRIBUTED_KEY,
} from "@/app/insights/teamInsights.mjs";
import type { AgentUsageRow, TeamInsights } from "@/lib/insights";
import {
  EmptyPlot,
  Plot,
  PlotCell,
  PlotGrid,
  PlotStack,
  RankedBars,
  StackedDailyColumns,
  type RankedEntry,
} from "./charts";
import { KpiTile } from "./KpiTile";
import { Notice, TeamInsightsPending } from "./TeamPanel";
import { useTeamInsights } from "./useTeamInsights";

/** "10/10 14:02" in the reader's zone; the raw string if it does not parse. */
function eventTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return `${parsed.getMonth() + 1}/${parsed.getDate()} ${parsed.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })}`;
}

const DENY = SERIES_COLORS[METRIC_COLOR.failed_turns];

/**
 * What the gateway's Cedar policies did, and to which team.
 *
 * Two sources, never merged into one number. The policy engine's CloudWatch
 * metrics count every decision (allow and deny, per mode) but carry no caller;
 * the ledger counts the denials the stream saw in a tool result and attributes
 * them to the caller's team. So the decision chart is "how much did policy
 * act", the team charts are "whose work did it stop", and a gap between them is
 * calls the stream never saw (another client on the gateway), not an error.
 *
 * LOG_ONLY "denials" passed. They are drawn apart from ENFORCE blocks, in the
 * muted mark, because stacking them would report a dry run as enforcement.
 */
export function PolicyPanel({ days, agents = [] }: { days: number; agents?: AgentUsageRow[] }) {
  const result = useTeamInsights(days);
  if (result.state !== "ready") return <TeamInsightsPending result={result} />;
  if (result.data.teams.length === 0) {
    return <Notice label="이 배포에는 팀이 없습니다. 조직 관리(/org)에서 팀을 만드세요." />;
  }
  return <Policy data={result.data} agents={agents} />;
}

function Policy({ data, agents }: { data: TeamInsights; agents: AgentUsageRow[] }) {
  const partialDay = data.partial_day ?? null;
  const d = data.gateway_decisions;
  const enforce = d.by_mode?.ENFORCE ?? { allow: 0, deny: 0 };
  const logOnly = d.by_mode?.LOG_ONLY ?? { allow: 0, deny: 0 };
  const enforced = enforce.allow + enforce.deny;
  const decisions = decisionRows(d.daily, partialDay);

  const series = teamSeries(data.teams);
  const denialRows = teamDailyRows(data.teams, data.unattributed, "policy_denials", partialDay);
  const teamDenials = data.teams.reduce((n, t) => n + t.policy_denials, 0);

  const byTeam: RankedEntry[] = data.teams
    .filter((t) => t.policy_denials > 0)
    .map((t) => ({
      name: t.label || t.team,
      value: t.policy_denials,
      display: `${t.policy_denials.toLocaleString("ko-KR")}회`,
      meta: t.turns > 0 ? `턴의 ${formatRate({ rate: t.policy_denials / t.turns })}` : undefined,
    }))
    .sort((a, b) => b.value - a.value);

  // Per tool from the policy engine's own metric, which also counts callers the
  // stream never saw; the ledger's per-team split is the heatmap below.
  const byTool: RankedEntry[] = Object.entries(d.by_tool ?? {})
    .map(([tool, n]) => ({ name: toolLabel(tool), value: n, display: `${n.toLocaleString("ko-KR")}회` }))
    .sort((a, b) => b.value - a.value);

  const matrix = denialMatrix(data.teams);
  const nameOf = new Map(agents.map((a) => [a.record_id, a.name]));
  const labelOf = new Map(data.teams.map((t) => [t.team, t.label || t.team]));

  return (
    <PlotStack>
      <PlotCell>
        <div className="grid grid-cols-2 gap-2 @3xl:grid-cols-4">
          <KpiTile
            label="ENFORCE 차단"
            value={data.sources.policy_metrics ? enforce.deny.toLocaleString("ko-KR") : "—"}
            hint={
              !data.sources.policy_metrics
                ? "정책 지표 미수집"
                : enforced > 0
                  ? `ENFORCE 결정의 ${formatRate({ rate: enforce.deny / enforced })}`
                  : "ENFORCE 결정 없음"
            }
            spark={decisions.length > 1 ? { data: decisions, dataKey: "enforce_deny", colorIndex: METRIC_COLOR.failed_turns } : undefined}
          />
          <KpiTile
            label="LOG_ONLY 거부 판정"
            value={data.sources.policy_metrics ? logOnly.deny.toLocaleString("ko-KR") : "—"}
            hint="통과했지만 정책이 거부했을 호출. ENFORCE 로 바꾸기 전에 보는 값"
          />
          <KpiTile
            label="허용"
            value={data.sources.policy_metrics ? d.allow.toLocaleString("ko-KR") : "—"}
            hint="모든 모드에서 정책이 허용한 툴 호출"
          />
          <KpiTile
            label="팀 귀속 거부"
            value={teamDenials.toLocaleString("ko-KR")}
            hint={`대화에서 확인된 거부 · 미귀속 ${data.unattributed.policy_denials.toLocaleString("ko-KR")}회`}
          />
        </div>
      </PlotCell>

      <PlotGrid>
        <PlotCell>
          <Plot
            title="일별 정책 거부"
            hint="게이트웨이 정책 엔진이 하루에 거부한 호출입니다. 붉은색은 실제로 막은 ENFORCE, 회색은 통과시킨 LOG_ONLY 판정입니다."
          >
            {data.sources.policy_metrics ? (
              <StackedDailyColumns
                data={decisions}
                series={[
                  { key: "enforce_deny", name: "ENFORCE 차단", color: DENY },
                  { key: "log_only_deny", name: "LOG_ONLY 거부 판정", color: MUTED_MARK },
                ]}
                empty="이 기간 정책 거부가 없습니다."
                integer
              />
            ) : (
              <EmptyPlot label="정책 지표가 아직 수집되지 않았습니다." />
            )}
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="팀별 일별 거부" hint="대화에서 확인된 거부를 호출한 사람의 팀으로 나눠 쌓았습니다.">
            <StackedDailyColumns
              data={denialRows}
              series={[...series, { key: UNATTRIBUTED_KEY, name: "미귀속", color: MUTED_MARK }]}
              empty="이 기간 대화에서 확인된 정책 거부가 없습니다."
              integer
            />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="팀별 거부" hint="숫자 옆은 팀 턴 대비 거부 비율입니다. 높으면 팀 허용 툴이 실제 업무보다 좁다는 신호입니다.">
            <RankedBars entries={byTeam} color={DENY} empty="거부된 팀이 없습니다." />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="툴별 차단" hint="정책 엔진 지표 기준입니다. 게이트웨이의 모든 호출자를 포함합니다.">
            <RankedBars entries={byTool} color={DENY} empty="툴별 거부 지표가 없습니다." />
          </Plot>
        </PlotCell>
      </PlotGrid>

      <PlotCell>
        <Plot title="팀 × 툴 거부" hint="어느 팀이 어느 툴에서 막혔는지입니다. 진할수록 많습니다.">
          {matrix.tools.length === 0 ? (
            <EmptyPlot label="이 기간 대화에서 확인된 정책 거부가 없습니다." />
          ) : (
            <div className="overflow-auto">
              <table className="text-xs">
                <thead>
                  <tr className="text-muted-foreground">
                    <th className="px-2 py-1 text-left font-medium">팀</th>
                    {matrix.tools.map((tool) => (
                      <th key={tool} className="px-2 py-1 text-center font-mono text-xxs font-medium" title={tool}>
                        {toolLabel(tool)}
                      </th>
                    ))}
                    <th className="px-2 py-1 text-right font-medium">합계</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {matrix.rows.map((row) => (
                    <tr key={row.team}>
                      <td className="whitespace-nowrap px-2 py-1 font-medium">{row.name}</td>
                      {row.cells.map((n, i) => (
                        <td key={matrix.tools[i]} className="p-0.5">
                          <div
                            className="flex h-7 min-w-14 items-center justify-center rounded"
                            // A tint of the alert hue scaled to the hottest cell; zero is the bare surface.
                            style={{
                              backgroundColor:
                                n > 0 ? `hsl(var(--chart-8) / ${0.15 + 0.6 * (n / matrix.max)})` : "hsl(var(--muted) / 0.4)",
                            }}
                          >
                            {n > 0 ? n : ""}
                          </div>
                        </td>
                      ))}
                      <td className="px-2 py-1 text-right">{row.total}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Plot>
      </PlotCell>

      <PlotCell>
        <Plot title="최근 정책 거부" hint="대화에서 확인된 거부 한 건이 한 줄입니다. 대화를 열어 어떤 요청이었는지 봅니다.">
          {data.recent_denials.length === 0 ? (
            <EmptyPlot label="거부 이벤트가 아직 없습니다." />
          ) : (
            <div className="max-h-64 overflow-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">시각</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">팀</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">사용자</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">에이전트</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">툴</th>
                    <th className="whitespace-nowrap px-2 py-1 text-right font-medium">대화</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_denials.map((e, i) => {
                    const agent = nameOf.get(e.agent_record_id) ?? e.agent_record_id ?? "—";
                    return (
                      <tr key={`${e.at}#${i}`} className="border-b border-border/50 last:border-0">
                        <td className="whitespace-nowrap px-2 py-1 tabular-nums">{eventTime(e.at)}</td>
                        <td className="whitespace-nowrap px-2 py-1">{e.team ? labelOf.get(e.team) ?? e.team : "미귀속"}</td>
                        <td className="whitespace-nowrap px-2 py-1 font-mono text-xxs">
                          {e.owner_sub ? subjectLabel(e.owner_sub, data.subjects) : "—"}
                        </td>
                        <td className="max-w-40 truncate px-2 py-1" title={agent}>
                          {agent}
                        </td>
                        <td className="px-2 py-1 font-mono text-xxs" title={e.tool_name}>
                          {toolLabel(e.tool_name)}
                        </td>
                        <td className="whitespace-nowrap px-2 py-1 text-right">
                          {e.thread_id ? (
                            <a
                              className="text-info underline"
                              href={`/?threadId=${encodeURIComponent(e.thread_id)}`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              열기
                            </a>
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Plot>
      </PlotCell>
    </PlotStack>
  );
}
