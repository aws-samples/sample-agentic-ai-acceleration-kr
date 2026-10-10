"use client";

import { ReactNode } from "react";
import { METRIC_COLOR } from "@/app/components/chartTheme";
import {
  type Composition,
  type InsightsSummary,
  type LeaderboardRow,
  type RecordInsights,
  type Telemetry,
} from "@/lib/insights";
import { KpiRow } from "./components/KpiRow";
import { AgentLeaderboard } from "./components/AgentLeaderboard";
import { BilledPanel } from "./components/BilledPanel";
import { CostCompositionPanel } from "./components/CostCompositionPanel";
import { ModelMixPanel } from "./components/ModelMixPanel";
import { TrendCharts } from "./components/TrendCharts";
import { ReusePanel } from "./components/ReusePanel";
import { TeamPanel } from "./components/TeamPanel";
import { UserPanel } from "./components/UserPanel";
import { QualityPanel } from "./components/QualityPanel";
import { TriagePanel } from "./components/TriagePanel";
import { GuardrailPanel } from "./components/GuardrailPanel";
import {
  FoldBand,
  PlotCell,
  PlotStack,
  RankedBars,
  type RankedEntry,
} from "./components/charts";
import { RecentTurns } from "./components/RecentTurns";
import { ThreadDrilldown } from "./components/ThreadDrilldown";
import {
  formatMicros,
  formatTokens,
  topNWithOther,
} from "./insightsFormat.mjs";

export type WidgetId =
  | "kpi"
  | "leaderboard"
  | "cost_composition"
  | "model_mix"
  | "billed"
  | "trend"
  | "reuse"
  | "users"
  | "guardrail"
  | "teams";

export interface WidgetConfig {
  title: string;
  defaultSpan: "half" | "full";
  render: () => ReactNode;
}

interface WidgetRegistryContext {
  summary: InsightsSummary | null;
  detail: RecordInsights | null;
  selected: string | null;
  onSelect: (id: string | null) => void;
  composition: Composition | null;
  /** Null until a reader fetches the metered CloudWatch tier (latency, errors). */
  telemetry: Telemetry | null;
  /** The window, so a widget that fetches its own data asks for the same one. */
  days: number;
}

export function createWidgetRegistry(ctx: WidgetRegistryContext): Record<WidgetId, WidgetConfig> {
  // The performance tier merged onto the ledger rows. `invocations` is deliberately
  // *not* taken from CloudWatch here: the collector already counted it per day for
  // the same window and the two would otherwise disagree by the vended lag.
  const vended = new Map(
    (ctx.telemetry?.agents ?? []).map((row) => [row.record_id, row]),
  );
  const rows: LeaderboardRow[] = (ctx.summary?.agents ?? []).map((row) => {
    const metrics = vended.get(row.record_id);
    if (!metrics) return row;
    const { record_id: _id, name: _name, invocations: _invocations, ...fields } = metrics;
    return { ...row, ...fields };
  });

  return {
    kpi: {
      title: "핵심 지표",
      defaultSpan: "full",
      render: () => (ctx.summary ? <KpiRow summary={ctx.summary} /> : null),
    },
    leaderboard: {
      title: "에이전트별 사용량",
      defaultSpan: "full",
      render: () =>
        ctx.summary ? (
          <AgentLeaderboard
            rows={rows}
            selected={ctx.selected}
            onSelect={ctx.onSelect}
            vendedLoaded={ctx.telemetry !== null}
            unclaimed={
              ctx.summary.cost.runtime.unclaimed_micros !== null &&
              ctx.summary.cost.runtime.unclaimed_micros !== undefined
                ? {
                    micros: ctx.summary.cost.runtime.unclaimed_micros,
                    runtimes: ctx.summary.cost.runtime.unclaimed_runtimes ?? [],
                  }
                : null
            }
          >
            {ctx.detail ? (
              <PlotStack>
                <PlotCell>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                    <span>
                      현재 모델{" "}
                      <span className="text-muted-foreground">
                        {ctx.detail.model_id ?? "확인 불가"}
                      </span>
                    </span>
                    <span>
                      입력 토큰{" "}
                      <span className="text-muted-foreground">
                        {formatTokens(
                          ctx.detail.totals.input_tokens,
                          ctx.detail.totals.unmeasured_turns,
                        )}
                      </span>
                    </span>
                    <span>
                      캐시 읽기{" "}
                      <span className="text-muted-foreground">
                        {formatTokens(
                          ctx.detail.totals.cache_read_tokens,
                          ctx.detail.totals.unmeasured_turns,
                        )}
                      </span>
                    </span>
                    <span>
                      모델 비용{" "}
                      <span className="text-muted-foreground">
                        {formatMicros(ctx.detail.model_cost_micros, {
                          unpriced: ctx.detail.totals.unpriced_turns,
                          unmeasured: ctx.detail.totals.unmeasured_turns,
                        })}
                      </span>
                    </span>
                    <span>
                      Runtime 비용{" "}
                      <span className="text-muted-foreground">
                        {formatMicros(ctx.detail.runtime_cost_micros)}
                      </span>
                    </span>
                    <a className="text-info underline" href="/registry">
                      레지스트리에서 보기
                    </a>
                  </div>
                </PlotCell>
                {/* Every band folds to its heading (`FoldBand`) and starts folded:
                    a click on the leaderboard should show the agent's headline
                    figures and an index of what else there is to read, not seven
                    open panels pushing each other off screen. Ordered by what an
                    admin comes here for — what the agent cost and did (models,
                    trend, turns, tools), then the traces behind it (threads), and
                    last the two metered judge-model tools (quality, triage) that
                    are opened once a week and scrolled past every other time. */}
                {ctx.detail.models.length > 1 && (
                  <FoldBand
                    title="이 에이전트가 쓴 모델"
                    hint="턴이 끝난 시점의 모델 기준입니다. 재배포로 모델이 바뀐 기간이면 둘 다 보입니다."
                    summary={`모델 ${ctx.detail.models.length}개`}
                    defaultOpen={false}
                  >
                    <table className="w-full text-xs">
                      <tbody>
                        {ctx.detail.models.map((model) => (
                          <tr key={model.model_id} className="border-b border-border/50 last:border-0">
                            <td className="py-1 pr-3 font-mono text-xxs">{model.model_id}</td>
                            <td className="py-1 pr-3 text-right tabular-nums">
                              {model.turns.toLocaleString("ko-KR")}턴
                            </td>
                            <td className="py-1 text-right tabular-nums">
                              {formatMicros(model.model_cost_micros, {
                                unpriced: model.registered ? 0 : model.turns,
                              })}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </FoldBand>
                )}
                <FoldBand
                  title="일별 추이"
                  summary={`${ctx.detail.daily.length}일`}
                  defaultOpen={false}
                  flush
                >
                  <TrendCharts
                    daily={ctx.detail.daily}
                    partialDay={ctx.detail.partial_day}
                  />
                </FoldBand>
                {/* Only turns whose tokens were recorded. Turns restored from
                    history carry no tokens and no cost, and a row of dashes tells
                    the reader nothing the counters above have not already said. */}
                {ctx.detail.turns.some((turn) => turn.measured) && (
                  <FoldBand
                    title="최근 턴"
                    hint="토큰이 기록된 최근 턴입니다. 표는 최신순, 오른쪽 차트는 같은 턴을 시간순으로 놓은 입력·출력 토큰과 비용입니다. 비용은 턴이 끝난 시점의 모델 요율로 계산한 값이고, 요율이 없는 턴은 비용 차트에서 빈 자리입니다."
                    summary={`턴 ${ctx.detail.turns.filter((turn) => turn.measured).length}`}
                    defaultOpen={false}
                  >
                    <RecentTurns turns={ctx.detail.turns} />
                  </FoldBand>
                )}
                {ctx.detail.tools.length > 0 && (
                  <FoldBand
                    title="이 에이전트의 툴 호출"
                    hint="이 기간 이 에이전트가 실제로 호출한 횟수입니다."
                    summary={`툴 ${ctx.detail.tools.length}개`}
                    defaultOpen={false}
                  >
                    <RankedBars
                      entries={
                        topNWithOther(
                          ctx.detail.tools.map((tool) => ({
                            name: tool.name,
                            value: tool.tool_calls,
                          })),
                          8,
                        ).map((entry: RankedEntry) => ({
                          ...entry,
                          display: `${entry.value.toLocaleString("ko-KR")}회`,
                        })) as RankedEntry[]
                      }
                      colorIndex={METRIC_COLOR.tool_calls}
                    />
                  </FoldBand>
                )}
                <FoldBand
                  title="최근 스레드"
                  /* The cost warning belongs here too. The CloudWatch button says in
                     so many words that it is metered, and then opening one of these
                     rows fires a Logs Insights StartQuery, which is billed on the
                     bytes it scans — the same page warning about one metered read
                     and staying silent about the other. */
                  hint="스레드를 열면 그 대화의 스팬 타임라인을 조회합니다 — CloudWatch Logs Insights 쿼리이므로 스캔한 용량만큼 요금이 붙고, 그래서 목록에서는 미리 읽지 않습니다. 가로축은 스레드 전체 구간이고, 막대는 그 안에서 각 스팬이 차지한 위치와 길이입니다. 색은 스팬의 종류이고, 무엇이 무엇인지는 타임라인 위 범례에 있습니다. 관리자가 아니면 자기 대화만 보입니다."
                  defaultOpen={false}
                >
                  <ThreadDrilldown recordId={ctx.detail.record_id} />
                </FoldBand>
                <FoldBand
                  title="품질 평가"
                  hint="AgentCore 배치 평가입니다. 판정 모델 호출마다 요금이 붙기 때문에 자동으로 돌지 않고, 관리자가 누를 때만 최근 스레드를 채점합니다."
                  defaultOpen={false}
                >
                  <QualityPanel recordId={ctx.detail.record_id} />
                </FoldBand>
                <FoldBand
                  title="실패 분석 · AgentCore Insights"
                  hint="세션 트레이스를 판정 모델이 읽어 실패 원인, 사용자 의도, 실행 패턴을 묶어 줍니다. 세션마다 요금이 붙기 때문에 자동으로 돌지 않고, 관리자가 누를 때만 최근 스레드를 분석합니다."
                  defaultOpen={false}
                >
                  <TriagePanel recordId={ctx.detail.record_id} />
                </FoldBand>
              </PlotStack>
            ) : (
              <div className="text-center text-sm text-muted-foreground">
                이 에이전트를 읽고 있습니다...
              </div>
            )}
          </AgentLeaderboard>
        ) : null,
    },
    cost_composition: {
      title: "비용 구성",
      defaultSpan: "half",
      render: () =>
        ctx.summary ? (
          <PlotStack>
            <CostCompositionPanel cost={ctx.summary.cost} />
          </PlotStack>
        ) : null,
    },
    model_mix: {
      title: "모델 믹스",
      defaultSpan: "half",
      render: () =>
        ctx.summary ? (
          <PlotStack>
            <ModelMixPanel models={ctx.summary.models} />
          </PlotStack>
        ) : null,
    },
    // The `PlotStack` lives here rather than inside each panel, so a panel returns
    // bands and stays composable.
    billed: {
      title: "AWS 청구서 대조",
      defaultSpan: "full",
      render: () =>
        ctx.summary ? (
          <PlotStack>
            <BilledPanel cost={ctx.summary.cost} agents={ctx.summary.agents} />
          </PlotStack>
        ) : null,
    },
    trend: {
      title: "일별 추이",
      defaultSpan: "full",
      render: () =>
        ctx.summary ? (
          <PlotStack>
            <TrendCharts
              daily={ctx.summary.daily}
              partialDay={ctx.summary.partial_day}
            />
          </PlotStack>
        ) : null,
    },
    reuse: {
      title: "구성 요소 사용",
      defaultSpan: "full",
      render: () =>
        ctx.composition ? (
          <PlotStack>
            <ReusePanel composition={ctx.composition} />
          </PlotStack>
        ) : null,
    },
    // Fetches its own data rather than taking it from `summary`: it is admin-only,
    // so a plain user's 403 must collapse this widget and nothing else.
    users: {
      title: "사용자별 사용량",
      defaultSpan: "full",
      render: () => (
        <PlotStack>
          <UserPanel days={ctx.days} />
        </PlotStack>
      ),
    },
    teams: {
      title: "팀별 사용량과 정책",
      defaultSpan: "full",
      render: () => (
        <PlotStack>
          <TeamPanel days={ctx.days} />
        </PlotStack>
      ),
    },
    guardrail: {
      title: "가드레일 개입",
      defaultSpan: "half",
      render: () =>
        ctx.summary ? (
          <GuardrailPanel
            guardrail={ctx.summary.guardrail ?? null}
            sources={ctx.summary.sources}
            agents={ctx.summary.agents}
            daily={ctx.summary.daily}
            partialDay={ctx.summary.partial_day}
            subjects={ctx.summary.subjects}
          />
        ) : null,
    },
  };
}
