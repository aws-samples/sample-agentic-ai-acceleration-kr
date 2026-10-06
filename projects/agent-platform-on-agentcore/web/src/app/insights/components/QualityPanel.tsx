"use client";

import { useCallback, useEffect, useState } from "react";
import { Play, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  fetchEvaluators,
  fetchRecordEvaluation,
  fetchRecordThreads,
  startEvaluation,
  InsightsApiError,
  type EvaluationRun,
  type EvaluationScore,
  type Evaluator,
} from "@/lib/insights";
import { EmptyPlot, RankedBars, type RankedEntry } from "./charts";

/**
 * Quality scores for one agent — the third subsystem that was built and never drawn.
 *
 * Six routes and four typed client functions existed for batch evaluations, and no
 * component called any of them: an LLM-as-judge run could be started and read
 * through the API and nowhere on the page. Quality is the one axis this dashboard
 * had no column for at all — every other figure says how *much* an agent was used,
 * none said whether the answers were any good.
 *
 * **Nothing here spends money without a click.** Reading the latest batch and the
 * evaluator catalogue are free; `startEvaluation` bills per judge-model call over
 * every session in the batch, so it is admin-only on the server and a button here,
 * with the session count stated before it is pressed.
 *
 * The panel is per-agent because AgentCore is: `serviceNames` caps at one entry, so
 * one batch covers exactly one runtime. That is why this lives in the leaderboard's
 * drill-down rather than as a page-level widget with an agent picker.
 */
function ScoreBars({ scores }: { scores: EvaluationScore[] }) {
  // Judged scores are 0..1 from AgentCore's built-in evaluators. An unscored
  // evaluator — every session failed, or none was eligible — is dropped rather than
  // drawn at zero: a bar at the floor reads as "scored badly", which is the opposite
  // of "not scored".
  const entries = scores
    .filter((score) => score.average_score !== null)
    .map((score) => ({
      // The AWS id when we have no word for it: an unlabelled row is still a
      // measurement, and inventing a Korean name for an evaluator we have not
      // measured would be a claim about what it scores.
      name: EVALUATOR_LABELS[score.evaluator_id] ?? score.evaluator_id,
      value: score.average_score as number,
      meta: `${score.evaluated}건 채점${score.failed > 0 ? ` · ${score.failed}건 실패` : ""}`,
      display: `${((score.average_score as number) * 100).toFixed(0)}점`,
    })) as RankedEntry[];

  if (entries.length === 0) {
    return <EmptyPlot label="채점된 항목이 없습니다." />;
  }
  return <RankedBars entries={entries} />;
}

const TERMINAL = new Set([
  "COMPLETED",
  "COMPLETED_WITH_ERRORS",
  "FAILED",
  "STOPPED",
]);

/**
 * The evaluators a run asks for, in preference order.
 *
 * **Not "every built-in one".** That is what this panel used to send, and it is
 * not a request AWS accepts: `StartBatchEvaluation` caps `evaluators` at 10 while
 * the account offers 18, so every press came back as a ValidationException that
 * the server relabelled "Evaluation service unavailable" — the button had never
 * once worked.
 *
 * The list is also not the first ten of the eighteen. Measured 2026-08-19 on a real
 * thread, each of these returned a score whose direction matches the bar it is
 * drawn as — `Builtin.Harmfulness` came back `1.0 / "Not Harmful"`, so higher is
 * better throughout. Deliberately left out:
 *
 * * `Builtin.Refusal` — scored `0.0 / "No"` for an answer that did not refuse, so
 *   its good value is the floor. Drawn beside nine higher-is-better bars it would
 *   read as the worst score on the panel.
 * * `Builtin.Trajectory*` and the tool/skill accuracy evaluators — they grade
 *   against an expected trajectory, which a batch over live conversations has no
 *   ground truth to supply.
 *
 * Filtered against the catalogue before use so an evaluator AWS retires drops out
 * rather than failing the whole run.
 */
const EVALUATOR_LABELS: Record<string, string> = {
  "Builtin.Correctness": "정확성",
  "Builtin.Faithfulness": "근거 충실성",
  "Builtin.Helpfulness": "도움 정도",
  "Builtin.ResponseRelevance": "질문과의 관련성",
  "Builtin.Coherence": "일관성",
  "Builtin.InstructionFollowing": "지시 이행",
  "Builtin.Conciseness": "간결성",
  "Builtin.GoalSuccessRate": "목표 달성",
  "Builtin.Harmfulness": "무해성",
};
const WANTED_EVALUATORS = Object.keys(EVALUATOR_LABELS);

export function QualityPanel({ recordId }: { recordId: string }) {
  const [run, setRun] = useState<EvaluationRun | { status: "none" } | null>(null);
  const [evaluators, setEvaluators] = useState<Evaluator[]>([]);
  const [threadCount, setThreadCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRun(await fetchRecordEvaluation(recordId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [recordId]);

  useEffect(() => {
    setRun(null);
    setThreadCount(null);
    void load();
    // The catalogue and the thread count are what the button needs to be honest
    // about what pressing it will do. Both are free reads.
    fetchEvaluators()
      .then((value) =>
        setEvaluators(
          // Intersected in `WANTED_EVALUATORS` order, not catalogue order, so the
          // set a press asks for is the measured one rather than whatever AWS
          // happens to list first.
          WANTED_EVALUATORS.map((id) =>
            value.evaluators.find(
              (evaluator) => evaluator.builtin && evaluator.evaluator_id === id,
            ),
          ).filter((evaluator): evaluator is Evaluator => Boolean(evaluator)),
        ),
      )
      .catch(() => setEvaluators([]));
    fetchRecordThreads(recordId, 20)
      .then((value) => setThreadCount(value.threads.length))
      .catch(() => setThreadCount(null));
  }, [recordId, load]);

  const start = async () => {
    if (evaluators.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const threads = await fetchRecordThreads(recordId, 20);
      if (threads.threads.length === 0) {
        setError("평가할 스레드가 없습니다.");
        return;
      }
      setRun(
        await startEvaluation(
          threads.threads.map((thread) => thread.thread_id),
          evaluators.map((evaluator) => evaluator.evaluator_id),
          recordId,
        ),
      );
    } catch (e) {
      if (e instanceof InsightsApiError && e.status === 403) {
        setForbidden(true);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const active = run && run.status !== "none" ? (run as EvaluationRun) : null;
  const running = Boolean(active && !TERMINAL.has(active.status));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {!forbidden && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy || running || evaluators.length === 0}
            onClick={() => void start()}
            title={
              evaluators.length === 0
                ? "평가자 카탈로그를 읽지 못했습니다."
                : "최근 스레드를 채점합니다. 판정 모델 호출만큼 요금이 붙습니다."
            }
          >
            <Play className={cn("size-3.5", busy && "animate-pulse")} />
            평가 실행
          </Button>
        )}
        {active && (
          <Button variant="ghost" size="sm" onClick={() => void load()}>
            <RefreshCw className="size-3.5" />
            상태 갱신
          </Button>
        )}
        {/* Stated before the button is pressed, not after: this is the one
            control in the drill-down that bills per session. */}
        {threadCount !== null && evaluators.length > 0 && !active && (
          <span className="text-xxs text-muted-foreground">
            스레드 {threadCount}개 × 평가자 {evaluators.length}개가 채점됩니다
          </span>
        )}
      </div>

      {forbidden && (
        <EmptyPlot label="평가 실행은 관리자만 할 수 있습니다. 점수는 누구나 볼 수 있습니다." />
      )}
      {error && <p className="text-xxs text-destructive">{error}</p>}

      {!run && <EmptyPlot label="평가 이력을 읽고 있습니다…" />}
      {run && run.status === "none" && (
        <EmptyPlot label="이 에이전트는 아직 평가된 적이 없습니다." />
      )}
      {active && active.status === "unavailable" && (
        <EmptyPlot label="평가 서비스를 읽지 못했습니다. 계정에 AgentCore Evaluations 가 켜져 있는지 확인해 주세요." />
      )}

      {active && active.status !== "unavailable" && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xxs text-muted-foreground">
            <span className="font-medium text-foreground">{active.status}</span>
            <span>
              세션 {active.sessions.completed}/{active.sessions.total} 완료
            </span>
            {active.sessions.failed > 0 && (
              <span className="text-warning">
                {active.sessions.failed}건 실패
              </span>
            )}
            {active.sessions.ignored > 0 && (
              <span>{active.sessions.ignored}건 제외</span>
            )}
            <span>{active.created_at}</span>
          </div>
          {/* AgentCore's own sentences, verbatim: "1 of 28 sessions failed" is
              more useful than any summary of it we could write, and rewriting it
              would put a claim in our voice that came from theirs. */}
          {active.errors.length > 0 && (
            <ul className="space-y-0.5 text-xxs text-warning">
              {active.errors.map((message, index) => (
                <li key={index}>{message}</li>
              ))}
            </ul>
          )}
          <ScoreBars scores={active.scores} />
        </div>
      )}
    </div>
  );
}
