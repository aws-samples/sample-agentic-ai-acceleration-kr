"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle, ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  fetchEvaluation,
  fetchEvaluators,
  fetchRecordThreads,
  InsightsApiError,
  startEvaluation,
  type EvaluationRun,
  type Evaluator,
  type RecordThread,
} from "@/lib/insights";
import {
  groupByLevel,
  isPending,
  scoreLabel,
  scoreTone,
  sessionSummary,
} from "@/app/components/evaluationSummary.mjs";
import { formatThreadTime, shortThreadId } from "@/app/insights/threadRows.mjs";

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
export function EvaluationPanel({ recordId }: { recordId: string }) {
  const [open, setOpen] = useState(false);
  const [evaluators, setEvaluators] = useState<Evaluator[] | null>(null);
  const [evaluatorsError, setEvaluatorsError] = useState<string | null>(null);
  const [threads, setThreads] = useState<RecordThread[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(
    new Set([
      "Builtin.Correctness",
      "Builtin.Helpfulness",
      "Builtin.GoalSuccessRate",
    ]),
  );
  const [selectedThreads, setSelectedThreads] = useState<Set<string>>(new Set());
  const [runningBatch, setRunningBatch] = useState<EvaluationRun | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // The catalogue and the threads, once, when the panel opens.
  useEffect(() => {
    if (!open || evaluators !== null) return;

    const load = async () => {
      setLoading(true);
      try {
        const result = await fetchEvaluators();
        if (!result.sources.evaluations) {
          setEvaluatorsError("평가 서비스를 사용할 수 없습니다");
          setEvaluators([]);
        } else {
          setEvaluators(result.evaluators);
        }
      } catch (e) {
        setEvaluatorsError(e instanceof Error ? e.message : String(e));
        setEvaluators([]);
      } finally {
        setLoading(false);
      }

      try {
        const listing = await fetchRecordThreads(recordId);
        setThreads(listing.threads);
        // The newest thread only. A default of "all of them" would make the
        // cheapest click the most expensive run.
        const newest = listing.threads[0];
        if (newest) setSelectedThreads(new Set([newest.thread_id]));
      } catch {
        setThreads([]);
      }
    };

    void load();
  }, [open, evaluators, recordId]);

  useEffect(() => {
    if (!runningBatch || !isPending(runningBatch.status)) {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
      return;
    }

    const poll = async () => {
      try {
        setRunningBatch(await fetchEvaluation(runningBatch.batch_id));
      } catch (e) {
        console.error("Failed to poll evaluation:", e);
      }
    };

    pollIntervalRef.current = setInterval(poll, 10000);
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
    };
  }, [runningBatch]);

  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  const toggleFrom = (
    set: Set<string>,
    apply: (next: Set<string>) => void,
    id: string,
  ) => {
    const next = new Set(set);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    apply(next);
  };

  const handleStart = async () => {
    if (selected.size === 0 || selectedThreads.size === 0) return;

    setStartError(null);
    try {
      const batch = await startEvaluation(
        Array.from(selectedThreads),
        Array.from(selected),
        // Attribution, so this tab can find the run again afterwards.
        recordId,
      );
      setRunningBatch(batch);
    } catch (e) {
      if (e instanceof InsightsApiError && e.status === 403) {
        setStartError("평가 실행은 관리자만 할 수 있습니다");
      } else if (e instanceof InsightsApiError && e.status === 422) {
        setStartError(e.message);
      } else {
        setStartError(e instanceof Error ? e.message : String(e));
      }
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className="flex items-center gap-1 py-1 text-xs font-medium hover:text-foreground"
        onClick={() => setOpen(true)}
        aria-expanded={false}
      >
        <ChevronRight className="size-3.5" />
        새 평가 실행
      </button>
    );
  }

  const groups = evaluators ? groupByLevel(evaluators) : [];

  return (
    <div className="space-y-3">
      <button
        type="button"
        className="flex items-center gap-1 py-1 text-xs font-medium"
        onClick={() => setOpen(false)}
        aria-expanded
      >
        <ChevronDown className="size-3.5" />
        새 평가 실행
      </button>

      {loading && <div className="text-xs text-muted-foreground">불러오는 중…</div>}

      {evaluatorsError && (
        <div className="flex items-start gap-2 rounded bg-muted p-2 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>{evaluatorsError}</span>
        </div>
      )}

      {!loading && !evaluatorsError && (
        <>
          {/* Threads first: the run is priced per thread, so the size of what is
              about to be spent should be visible before the evaluator list. */}
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">
              대상 스레드
            </div>
            {threads === null ? (
              <p className="text-xs text-muted-foreground">스레드를 읽고 있습니다…</p>
            ) : threads.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                이 에이전트로 열린 스레드가 없어 평가할 대상이 없습니다.
              </p>
            ) : (
              <ul className="space-y-1">
                {threads.map((thread) => (
                  <li key={thread.thread_id}>
                    <label className="flex cursor-pointer items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        className="size-3 cursor-pointer"
                        checked={selectedThreads.has(thread.thread_id)}
                        onChange={() =>
                          toggleFrom(
                            selectedThreads,
                            setSelectedThreads,
                            thread.thread_id,
                          )
                        }
                      />
                      <span className="font-mono text-xxs">
                        {shortThreadId(thread.thread_id)}
                      </span>
                      <span className="tabular-nums text-muted-foreground">
                        {formatThreadTime(thread.updated_at)} · 턴{" "}
                        {thread.turns.toLocaleString("ko-KR")}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="space-y-2">
            {groups.map((group: { level: string; label: string; evaluators: Evaluator[] }) => (
              <div key={group.level}>
                <div className="mb-1 text-xs font-medium text-muted-foreground">
                  {group.label}
                </div>
                <div className="space-y-1">
                  {group.evaluators.map((evaluator: Evaluator) => (
                    <label
                      key={evaluator.evaluator_id}
                      className="flex cursor-pointer items-center gap-2 text-xs"
                    >
                      <input
                        type="checkbox"
                        className="size-3 cursor-pointer"
                        checked={selected.has(evaluator.evaluator_id)}
                        onChange={() =>
                          toggleFrom(selected, setSelected, evaluator.evaluator_id)
                        }
                      />
                      <span>{evaluator.name}</span>
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-start gap-2">
            <button
              type="button"
              onClick={() => void handleStart()}
              disabled={
                selected.size === 0 ||
                selectedThreads.size === 0 ||
                (runningBatch !== null && isPending(runningBatch.status))
              }
              className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              평가 실행
            </button>
            <span className="mt-1.5 text-xs text-muted-foreground">
              평가는 판정 모델을 호출하므로 비용이 발생합니다. 스레드{" "}
              {selectedThreads.size}개 · 평가자 {selected.size}개
            </span>
          </div>
        </>
      )}

      {startError && (
        <div className="flex items-start gap-2 rounded bg-muted p-2 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>{startError}</span>
        </div>
      )}

      {runningBatch && (
        <div className="space-y-2 rounded bg-muted p-2">
          <div className="flex items-center gap-2 text-xs">
            {isPending(runningBatch.status) ? (
              <>
                <div className="size-4 animate-spin rounded-full border-2 border-muted-foreground border-t-foreground" />
                <span>평가 진행 중…</span>
              </>
            ) : (
              <>
                <CheckCircle className="size-4 text-success" />
                <span>평가 완료</span>
              </>
            )}
          </div>

          <div className="text-xs text-muted-foreground">
            {sessionSummary(runningBatch.sessions)}
          </div>

          {runningBatch.scores.length > 0 && (
            // Each evaluator keeps its own score and is never averaged with the
            // others: different levels measure incompatible scales.
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-muted-foreground/20">
                  <th className="px-1 py-1 text-left font-medium">평가자</th>
                  <th className="px-1 py-1 text-right font-medium">점수</th>
                  <th className="px-1 py-1 text-right font-medium">평가됨</th>
                </tr>
              </thead>
              <tbody>
                {runningBatch.scores.map((score) => {
                  const evaluator = evaluators?.find(
                    (candidate) => candidate.evaluator_id === score.evaluator_id,
                  );
                  const tone = scoreTone(score.average_score);
                  return (
                    <tr
                      key={score.evaluator_id}
                      className="border-b border-muted-foreground/10"
                    >
                      <td className="px-1 py-1">
                        {evaluator?.name || score.evaluator_id}
                      </td>
                      <td
                        className={cn(
                          "px-1 py-1 text-right",
                          tone === "good"
                            ? "text-success"
                            : tone === "warn"
                              ? "text-warning"
                              : tone === "bad"
                                ? "text-destructive"
                                : "text-muted-foreground",
                        )}
                      >
                        {scoreLabel(score.average_score)}
                      </td>
                      <td className="px-1 py-1 text-right text-muted-foreground">
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
    </div>
  );
}
