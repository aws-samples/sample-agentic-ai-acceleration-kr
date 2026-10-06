"use client";

import { useEffect, useState } from "react";

import { LoadingState } from "@/app/components/PageHeader";
import { TrendCharts } from "@/app/insights/components/TrendCharts";
import { formatTokens } from "@/app/insights/insightsFormat.mjs";
import {
  fetchRecordInsights,
  fetchRecordEvaluation,
  InsightsApiError,
  type RecordInsights,
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
 */
export function RecordUsageTab({ recordId }: { recordId: string }) {
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
    fetchRecordEvaluation(recordId)
      .then((value) => live && setEvaluation(value))
      .catch((e) => {
        console.error("Failed to fetch record evaluation:", e);
      });
    return () => {
      live = false;
    };
  }, [recordId]);

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
  // An MCP/gateway record is called *through*, not run, so its own turn, user and
  // token counters are always zero. `reach` is present exactly for those records,
  // and it is the honest measure: who attaches this server and how much traffic
  // they carried. Tool calls below are attributed back to it from those agents.
  const reach = insights.reach;
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
        <dl className="grid grid-cols-3 gap-2 text-xs">
          <div>
            <dt className="text-muted-foreground">연결된 에이전트</dt>
            <dd className="tabular-nums">
              {reach.agents.toLocaleString("ko-KR")}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">처리한 턴</dt>
            <dd className="tabular-nums">
              {reach.turns.toLocaleString("ko-KR")}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">툴 호출</dt>
            <dd className="tabular-nums">
              {toolCallTotal.toLocaleString("ko-KR")}
            </dd>
          </div>
        </dl>
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
      <EvaluationPanel recordId={recordId} />
      {insights.tools.length > 0 && (
        <ul className="flex flex-wrap gap-1 text-xxs text-muted-foreground">
          {insights.tools.map((tool) => (
            <li key={tool.name} className="rounded bg-muted px-1.5 py-0.5">
              {tool.name} {tool.tool_calls}회
            </li>
          ))}
        </ul>
      )}
      {/* The daily trend is the record's own turns over time, which a gateway does
          not have — its `reach` traffic belongs to the agents, shown on their own
          pages. So the chart is for agent records only. */}
      {!reach && <TrendCharts daily={insights.daily} />}
    </div>
  );
}
