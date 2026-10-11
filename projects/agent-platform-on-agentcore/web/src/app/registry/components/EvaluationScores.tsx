"use client";

import { CheckCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  type EvaluationRun,
  type Evaluator,
} from "@/lib/insights";
import {
  evaluatorLabel,
  isPending,
  scoreLabel,
  scoreTone,
  sessionSummary,
} from "@/app/components/evaluationSummary.mjs";
import { EmptyPlot } from "@/app/insights/components/charts";
import { METRIC_COLOR, SERIES_COLORS } from "@/app/components/chartTheme";

/**
 * One run's scores, the same way wherever a run is shown: the record's latest
 * run in the Usage tab and a run just started from the launcher beneath it.
 *
 * Each evaluator keeps its own row and is never averaged with the others —
 * different levels measure incompatible scales — but every score is on 0..1, so
 * a bar per row reads at a glance and the number carries the tone. A null score
 * (an evaluator that failed on every session) draws no bar: it was never
 * computed, which is not the same as 0.
 */
export function EvaluationScores({
  run,
  evaluators,
}: {
  run: EvaluationRun;
  evaluators?: Evaluator[] | null;
}) {
  const pending = isPending(run.status);
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        {pending ? (
          <div className="size-3.5 animate-spin rounded-full border-2 border-muted-foreground border-t-foreground" />
        ) : (
          <CheckCircle className="size-3.5 text-success" />
        )}
        <span>{sessionSummary(run.sessions)}</span>
      </div>
      {run.scores.length === 0 ? (
        pending ? null : <EmptyPlot label="점수가 기록된 평가자가 없습니다." />
      ) : (
        <ul className="space-y-1.5">
          {run.scores.map((score) => {
            const tone = scoreTone(score.average_score);
            const value = score.average_score;
            return (
              <li key={score.evaluator_id} className="flex items-center gap-2 px-1 py-0.5">
                <span
                  className="w-28 shrink-0 truncate text-xs @2xl:w-44"
                  title={score.evaluator_id}
                >
                  {evaluatorLabel(score.evaluator_id, evaluators)}
                </span>
                <span className="relative h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
                  {value !== null && value !== undefined && (
                    <span
                      className="absolute inset-y-0 left-0 rounded-full"
                      style={{
                        width: `${Math.max(value * 100, value > 0 ? 1.5 : 0)}%`,
                        backgroundColor:
                          tone === "bad" ? "hsl(var(--destructive))" : SERIES_COLORS[METRIC_COLOR.turns],
                      }}
                    />
                  )}
                </span>
                <span
                  className={cn(
                    "w-9 shrink-0 text-right text-xs tabular-nums",
                    tone === "good"
                      ? "text-success"
                      : tone === "warn"
                        ? "text-warning"
                        : tone === "bad"
                          ? "text-destructive"
                          : "text-muted-foreground",
                  )}
                >
                  {scoreLabel(value)}
                </span>
                <span className="w-9 shrink-0 text-right text-xxs tabular-nums text-muted-foreground">
                  {score.evaluated}/{score.evaluated + score.failed}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * Start an evaluation of this agent, beside the approval decision it informs.
 *
 * Moved here from the chat, where it was mounted under every assistant message
 * even though a batch covers a *thread* — a ten-turn conversation grew ten
 * identical "평가" panels, any of which would launch the same paid run.
 *
 * Two things this placement fixes rather than merely relocates:
 *
 *  - The run is attributed to the record (`record_id`), so the scores show up in
 *    this tab's "latest evaluation" afterwards. The chat call omitted it, and the
 *    result was a run nothing could find again.
 *  - The threads are chosen deliberately. AWS caps a batch at one agent
 *    (`serviceNames`), and every thread here belongs to this record by
 *    construction, so a selection cannot straddle two agents and 422.
 *
 * Fetches nothing until opened: the evaluator catalogue and the thread list are
 * both round trips that a closed panel has no use for.
 */
