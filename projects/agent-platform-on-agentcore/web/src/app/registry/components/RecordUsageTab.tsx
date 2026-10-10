"use client";

import { useEffect, useState } from "react";

import { LoadingState } from "@/app/components/PageHeader";
import { METRIC_COLOR } from "@/app/components/chartTheme";
import {
  DailyColumns,
  DonutShare,
  EmptyPlot,
  Plot,
  PlotCell,
  PlotGrid,
  RankedBars,
} from "@/app/insights/components/charts";
import { TrendCharts } from "@/app/insights/components/TrendCharts";
import { dailySeries, formatTokens } from "@/app/insights/insightsFormat.mjs";
import {
  fetchRecordInsights,
  fetchRecordEvaluation,
  InsightsApiError,
  type RecordInsights,
  type RecordReach,
  type EvaluationRun,
} from "@/lib/insights";
import {
  scoreLabel,
  scoreTone,
  sessionSummary,
} from "@/app/components/evaluationSummary.mjs";
import { EvaluationPanel } from "./EvaluationPanel";
import { cn } from "@/lib/utils";

/**
 * Usage figures where the approval decision is actually made.
 *
 * `APPROVED` on its own records that a curator pressed a button; nothing behind
 * it says the agent is used, healthy or affordable. This panel is the answer to
 * that, so it sits beside the approval control rather than a page away.
 *
 * `isAgent` decides whether evaluation belongs here. A batch evaluation runs over
 * the threads one agent answered in, so a skill or MCP server — which owns no
 * threads — can never have one. The tab used to offer the launcher anyway and
 * reported "no threads opened with this agent" on a skill, which read as a
 * failure; now it names the agents that reach the record and sends the reader
 * there, where the scores actually live.
 */
export function RecordUsageTab({
  recordId,
  isAgent = true,
  onOpenRecord,
}: {
  recordId: string;
  isAgent?: boolean;
  onOpenRecord?: (recordId: string) => void;
}) {
  const [days, setDays] = useState(30);
  const [insights, setInsights] = useState<RecordInsights | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationRun | { status: "none" } | null>(null);
  const [unconfigured, setUnconfigured] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setInsights(null);
    setError(null);
    fetchRecordInsights(recordId, days)
      .then((value) => live && setInsights(value))
      .catch((e) => {
        if (!live) return;
        // A 501 is the one non-error outcome: this environment has no usage
        // table, which is a state to render, not a failure. Every other
        // rejection — 500, 403, a network drop, a timeout — must surface, or the
        // spinner below runs forever with nothing telling the reader why.
        if (e instanceof InsightsApiError && e.status === 501) {
          setUnconfigured(true);
        } else {
          setError(e instanceof Error ? e.message : "사용량을 불러오지 못했습니다.");
        }
      });
    return () => {
      live = false;
    };
  }, [recordId, days]);

  useEffect(() => {
    let live = true;
    setEvaluation(null);
    // Not fetched for a skill or MCP record: the answer is always "none" and
    // showing it would claim the record could have been evaluated.
    if (!isAgent) return;
    fetchRecordEvaluation(recordId)
      .then((value) => live && setEvaluation(value))
      .catch((e) => {
        console.error("Failed to fetch record evaluation:", e);
      });
    return () => {
      live = false;
    };
  }, [recordId, isAgent]);

  if (unconfigured) {
    return (
      <p className="text-xs text-muted-foreground">
        이 환경에는 사용량 테이블이 설정되어 있지 않아 기록이 없습니다.
      </p>
    );
  }
  if (error) {
    return <p className="text-xs text-destructive">{error}</p>;
  }
  if (!insights) return <LoadingState label="사용량을 읽고 있습니다" />;

  const totals = insights.totals;
  // Narrowed once, by the discriminant rather than by a cast. `EvaluationStatus`
  // is a literal union, so `status !== "none"` is enough — with `status: string`
  // it was not, and every use site carried an `as EvaluationRun` instead.
  const run = evaluation && evaluation.status !== "none" ? evaluation : null;
  // A skill or MCP record is reached, not run, so its own turn, user and token
  // counters are always zero. `reach` is present exactly for those records, and
  // it is the honest measure: which agents reach it and how much traffic they
  // carried. For an MCP server the turns passed *through* it (`traffic`) and the
  // tool calls below are its own; for a skill nothing counts a read, so the turns
  // are those of the agents that attach it (`reach`) and there are no calls.
  const reach = insights.reach;
  const isSkillReach = reach?.metric === "reach";
  const toolCallTotal = insights.tools.reduce(
    (sum, tool) => sum + tool.tool_calls,
    0,
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs">
        {[7, 30].map((window) => (
          <button
            key={window}
            type="button"
            className={
              days === window ? "font-semibold" : "text-muted-foreground"
            }
            onClick={() => setDays(window)}
          >
            {window}일
          </button>
        ))}
      </div>
      {reach ? (
        <div className="space-y-2">
          <dl className="grid grid-cols-3 gap-2 text-xs">
            <div>
              <dt className="text-muted-foreground">연결된 에이전트</dt>
              <dd className="tabular-nums">
                {reach.agents.toLocaleString("ko-KR")}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">
                {isSkillReach ? "도달한 턴" : "처리한 턴"}
              </dt>
              <dd className="tabular-nums">
                {reach.turns.toLocaleString("ko-KR")}
              </dd>
            </div>
            {/* A skill has no call count — nothing counts it being read — so the
                tile is left out rather than shown as a 0 that looks measured. */}
            {!isSkillReach && (
              <div>
                <dt className="text-muted-foreground">툴 호출</dt>
                <dd className="tabular-nums">
                  {toolCallTotal.toLocaleString("ko-KR")}
                </dd>
              </div>
            )}
          </dl>
          {isSkillReach && (
            <p className="text-xxs text-muted-foreground">
              스킬은 호출 카운트가 없어 이 스킬을 붙인 에이전트가 처리한 턴으로 셉니다.
            </p>
          )}
        </div>
      ) : (
        <dl className="grid grid-cols-2 gap-2 text-xs lg:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">턴</dt>
            <dd className="tabular-nums">
              {(totals.turns ?? 0).toLocaleString("ko-KR")}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">사용자</dt>
            <dd className="tabular-nums">{insights.distinct_users}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">입력 토큰</dt>
            <dd className="tabular-nums">
              {formatTokens(totals.input_tokens, totals.unmeasured_turns)}
            </dd>
          </div>
          {/* Tool calls, not a cost estimate: the per-token figure that used to
              sit here was built from hand-transcribed list prices for models the
              Price List API does not publish. */}
          <div>
            <dt className="text-muted-foreground">툴 호출</dt>
            <dd className="tabular-nums">
              {(totals.tool_calls ?? 0).toLocaleString("ko-KR")}
            </dd>
          </div>
        </dl>
      )}

      {/* Evaluation scores */}
      {run && (
        <div className="rounded bg-muted p-2 space-y-2">
          <div className="text-xs font-medium text-muted-foreground">
            {sessionSummary(run.sessions)}
          </div>
          {run.scores && run.scores.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-muted-foreground/20">
                  <th className="text-left py-1 px-1 font-medium">평가자</th>
                  <th className="text-right py-1 px-1 font-medium">점수</th>
                  <th className="text-right py-1 px-1 font-medium">평가됨</th>
                </tr>
              </thead>
              <tbody>
                {run.scores.map((score) => {
                  const tone = scoreTone(score.average_score);
                  return (
                    <tr key={score.evaluator_id} className="border-b border-muted-foreground/10">
                      <td className="py-1 px-1 text-muted-foreground">
                        {score.evaluator_id}
                      </td>
                      <td
                        className={cn(
                          "text-right py-1 px-1",
                          tone === "good"
                            ? "text-green-600"
                            : tone === "warn"
                              ? "text-yellow-600"
                              : tone === "bad"
                                ? "text-red-600"
                                : "text-muted-foreground",
                        )}
                      >
                        {scoreLabel(score.average_score)}
                      </td>
                      <td className="text-right py-1 px-1 text-muted-foreground">
                        {score.evaluated}/{score.evaluated + score.failed}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
      {evaluation && evaluation.status === "none" && (
        <div className="text-xs text-muted-foreground">평가한 적 없음</div>
      )}
      {/* The launcher sits with the scores it produces. It used to live under
          every assistant message in the chat, which repeated it once per turn and
          started runs nothing attributed back to this record. */}
      {isAgent && <EvaluationPanel recordId={recordId} />}

      {/* Who reaches this record, and what was called through it. The agents are
          a ranked list because the question is "who, and how much" — and each row
          opens that agent, which is where evaluation lives (a batch runs over one
          agent's threads, so a skill or MCP record can never have one of its own).
          Tools are a donut because the question there is share of the whole. A
          skill has no calls to share out, so its cell is left out rather than
          drawn empty. */}
      {(reach || insights.tools.length > 0) && (
        <PlotGrid>
          {reach && (
            <PlotCell>
              <Plot
                title="연결된 에이전트"
                hint={
                  isSkillReach
                    ? "이 스킬을 붙인 에이전트와 그 턴 수입니다. 평가는 각 에이전트에서 봅니다."
                    : "이 서버를 호출한 에이전트와 그 턴 수입니다. 평가는 각 에이전트에서 봅니다."
                }
              >
                <RankedBars
                  entries={reach.agent_records.map((agent) => ({
                    id: agent.record_id,
                    name: agent.name ?? agent.record_id,
                    value: agent.turns,
                    display: `${agent.turns.toLocaleString("ko-KR")}턴`,
                  }))}
                  onSelect={onOpenRecord}
                  colorIndex={METRIC_COLOR.turns}
                  empty="이 기간에 이 레코드에 도달한 에이전트가 없습니다."
                />
              </Plot>
            </PlotCell>
          )}
          {!isSkillReach && (
            <PlotCell>
              <Plot
                title="툴별 호출"
                hint={
                  reach
                    ? "이 서버의 툴이 호출된 횟수의 비중입니다."
                    : "이 에이전트가 호출한 툴의 비중입니다."
                }
              >
                {insights.tools.length > 0 ? (
                  <DonutShare
                    segments={insights.tools.map((tool) => ({
                      name: tool.name,
                      value: tool.tool_calls,
                    }))}
                    totalLabel="회"
                  />
                ) : (
                  <EmptyPlot label="이 기간에 호출된 툴이 없습니다." />
                )}
              </Plot>
            </PlotCell>
          )}
        </PlotGrid>
      )}
      {/* The daily trend is the record's own turns for an agent, and the same
          reach rolled up per day for a skill or MCP record — `insights.daily` is
          the record's own counters, which for those are empty. */}
      {reach ? (
        <ReachTrend reach={reach} partialDay={insights.partial_day} />
      ) : (
        <TrendCharts daily={insights.daily} />
      )}
    </div>
  );
}

/**
 * The reach figures above, day by day. Turns are columns like the agent trend;
 * for a skill they are the turns of the agents that attach it, said so in the
 * hint, and the calls plot is left out rather than drawn flat at zero. An
 * MCP record gets both: the turns that passed through it and its own calls.
 */
function ReachTrend({
  reach,
  partialDay,
}: {
  reach: RecordReach;
  partialDay?: string | null;
}) {
  if (reach.daily.length === 0) return null;
  const series = dailySeries(reach.daily, partialDay) as Array<Record<string, unknown>>;
  const isSkill = reach.metric === "reach";
  return (
    <PlotGrid>
      <PlotCell>
        <Plot
          title={isSkill ? "일별 도달 턴" : "일별 턴"}
          hint={
            isSkill
              ? "이 스킬을 붙인 에이전트가 하루에 처리한 턴 수입니다."
              : "이 서버를 호출한 에이전트가 하루에 처리한 턴 수입니다."
          }
        >
          <DailyColumns data={series} dataKey="turns" name="턴" colorIndex={METRIC_COLOR.turns} />
        </Plot>
      </PlotCell>
      {!isSkill && (
        <PlotCell>
          <Plot title="일별 툴 호출" hint="이 서버의 툴이 하루에 호출된 횟수입니다.">
            <DailyColumns
              data={series}
              dataKey="tool_calls"
              name="툴 호출"
              colorIndex={METRIC_COLOR.tool_calls}
            />
          </Plot>
        </PlotCell>
      )}
    </PlotGrid>
  );
}
