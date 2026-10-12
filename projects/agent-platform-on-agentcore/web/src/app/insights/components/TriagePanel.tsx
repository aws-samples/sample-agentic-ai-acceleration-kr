"use client";

import { useCallback, useEffect, useState } from "react";
import { Play, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  fetchRecordAnalysis,
  fetchRecordThreads,
  startAnalysis,
  InsightsApiError,
  type AnalysisRun,
  type InsightSessionHit,
  type InsightsTree,
} from "@/lib/insights";
import { shortThreadId } from "@/app/insights/threadRows.mjs";
import {
  clusterEntries,
  distinctAffectedSessions,
  failureRows,
} from "@/app/insights/triageRows.mjs";
import { EmptyPlot, RankedBars, type RankedEntry } from "./charts";

/**
 * AgentCore insights for one agent — *why* it fails, *what* people ask it, *how*
 * it goes about answering. The quality panel above says how well the answers
 * scored; this one is the triage that follows a bad score.
 *
 * **Nothing here spends money without a click.** Reading the latest run is free;
 * `startAnalysis` bills per session analysed, so it is admin-only on the server and
 * a button here, with the session count stated before it is pressed.
 *
 * **Every number is AgentCore's.** The tree is printed with the service's own
 * per-cluster counts and nothing is summed across clusters — a session can sit in
 * several failure categories, so a total of category counts is not a count of
 * sessions. The one total shown, "sessions with at least one failure", is a
 * distinct count over session ids, and it is withheld (not zeroed) when the ids
 * were stripped for a plain user.
 */
const TERMINAL = new Set([
  "COMPLETED",
  "COMPLETED_WITH_ERRORS",
  "FAILED",
  "STOPPED",
]);

const INSIGHT_LABELS: Record<string, string> = {
  "Builtin.Insight.FailureAnalysis": "실패 분석",
  "Builtin.Insight.UserIntent": "사용자 의도",
  "Builtin.Insight.ExecutionSummary": "실행 패턴",
};

const MAX_SESSIONS_SHOWN = 6;

function SessionChips({ hits }: { hits: InsightSessionHit[] }) {
  if (hits.length === 0) return null;
  const shown = hits.slice(0, MAX_SESSIONS_SHOWN);
  return (
    <span className="flex flex-wrap items-center gap-1">
      {shown.map((hit) => (
        <span
          key={hit.session_id}
          // The thread when we could resolve one; the session id AgentCore
          // reported otherwise, so the row is still traceable in CloudWatch.
          title={hit.explanation ?? hit.thread_id ?? hit.session_id}
          className="rounded bg-muted px-1 font-mono text-xxs text-muted-foreground"
        >
          {shortThreadId(hit.thread_id ?? hit.session_id)}
        </span>
      ))}
      {hits.length > shown.length && (
        <span className="text-xxs text-muted-foreground">
          +{hits.length - shown.length}
        </span>
      )}
    </span>
  );
}

function FailureTree({ tree }: { tree: InsightsTree }) {
  const rows = failureRows(tree.failures);
  if (rows.length === 0) {
    return (
      <EmptyPlot
        label={
          tree.requested.includes("Builtin.Insight.FailureAnalysis")
            ? "분석된 세션에서 실패 패턴이 발견되지 않았습니다."
            : "이 실행은 실패 분석을 요청하지 않았습니다."
        }
      />
    );
  }
  return (
    <ul className="space-y-1">
      {rows.map((row) => (
        <li
          key={row.key}
          className={cn(
            "rounded px-1 py-0.5",
            row.depth === 0 && "bg-muted/40",
          )}
          style={{ marginLeft: `${row.depth * 0.9}rem` }}
        >
          <div className="flex items-baseline justify-between gap-2">
            <span
              className={cn(
                "min-w-0 truncate text-xs",
                row.depth === 0 && "font-medium",
                row.depth === 2 && "text-muted-foreground",
              )}
              title={row.description ?? row.name}
            >
              {row.name}
            </span>
            {/* AgentCore's count for this cluster, verbatim. Sessions overlap
                between clusters, so these are never added up. */}
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {row.count}세션
            </span>
          </div>
          {row.depth === 2 && (
            <div className="mt-0.5 space-y-0.5 text-xxs leading-snug">
              {row.rootCause && (
                <p className="text-muted-foreground">{row.rootCause}</p>
              )}
              {row.recommendation && (
                <p>
                  <span className="font-medium">권고 </span>
                  {row.recommendation}
                </p>
              )}
              <SessionChips hits={row.sessions as InsightSessionHit[]} />
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function ClusterBars({
  clusters,
  insightId,
  tree,
  colorIndex,
}: {
  clusters: InsightsTree["user_intents"];
  insightId: string;
  tree: InsightsTree;
  colorIndex: number;
}) {
  const entries = clusterEntries(clusters) as RankedEntry[];
  if (!tree.requested.includes(insightId)) {
    return <EmptyPlot label={`이 실행은 ${INSIGHT_LABELS[insightId]}을 요청하지 않았습니다.`} />;
  }
  return (
    <div className="space-y-2">
      <RankedBars
        entries={entries}
        colorIndex={colorIndex}
        empty="분석된 세션에서 묶인 패턴이 없습니다."
      />
      {clusters.some((cluster) => cluster.description) && (
        <details className="text-xxs text-muted-foreground">
          <summary className="cursor-pointer select-none">설명 보기</summary>
          <dl className="mt-1 space-y-1">
            {clusters.map((cluster) => (
              <div key={cluster.cluster_id ?? cluster.name}>
                <dt className="font-medium text-foreground">{cluster.name}</dt>
                <dd>{cluster.description}</dd>
                {cluster.sessions.length > 0 && (
                  <dd className="mt-0.5">
                    <SessionChips hits={cluster.sessions} />
                  </dd>
                )}
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}

export function TriagePanel({ recordId }: { recordId: string }) {
  const [run, setRun] = useState<AnalysisRun | { status: "none" } | null>(null);
  const [threadCount, setThreadCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRun(await fetchRecordAnalysis(recordId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [recordId]);

  useEffect(() => {
    setRun(null);
    setThreadCount(null);
    void load();
    // Stated before the button is pressed: this is the figure the bill scales with.
    fetchRecordThreads(recordId, 20)
      .then((value) => setThreadCount(value.threads.length))
      .catch(() => setThreadCount(null));
  }, [recordId, load]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const threads = await fetchRecordThreads(recordId, 20);
      if (threads.threads.length === 0) {
        setError("분석할 스레드가 없습니다.");
        return;
      }
      setRun(
        await startAnalysis(
          threads.threads.map((thread) => thread.thread_id),
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

  const active = run && run.status !== "none" ? (run as AnalysisRun) : null;
  const running = Boolean(active && !TERMINAL.has(active.status));
  // Only a finished run has findings. The Start response already echoes an
  // `insights` object with three empty lists (measured 2026-09-23), and a run
  // that FAILED has the same shape — drawn as a tree, either reads as "no
  // failure patterns were found", which is the one claim this panel must not
  // make by accident.
  const finished = Boolean(
    active && (active.status === "COMPLETED" || active.status === "COMPLETED_WITH_ERRORS"),
  );
  const tree = finished ? (active?.insights ?? null) : null;
  const failedSessions = tree ? distinctAffectedSessions(tree) : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {!forbidden && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy || running}
            onClick={() => void start()}
            title="최근 스레드의 세션을 분석합니다. 세션 수만큼 요금이 붙습니다."
          >
            <Play className={cn("size-3.5", busy && "animate-pulse")} />
            분석 실행
          </Button>
        )}
        {active && (
          <Button variant="ghost" size="sm" onClick={() => void load()}>
            <RefreshCw className="size-3.5" />
            상태 갱신
          </Button>
        )}
        {threadCount !== null && !active && (
          <span className="text-xxs text-muted-foreground">
            스레드 {threadCount}개의 세션이 분석됩니다
          </span>
        )}
      </div>

      {forbidden && (
        <EmptyPlot label="분석 실행은 관리자만 할 수 있습니다. 결과는 누구나 볼 수 있습니다." />
      )}
      {error && <p className="text-xxs text-destructive">{error}</p>}

      {!run && <EmptyPlot label="분석 이력을 읽고 있습니다…" />}
      {run && run.status === "none" && (
        <EmptyPlot label="이 에이전트는 아직 분석된 적이 없습니다." />
      )}
      {active && active.status === "unavailable" && (
        <EmptyPlot label="분석 서비스를 읽지 못했습니다. 계정에 AgentCore Evaluations 가 켜져 있는지 확인해 주세요." />
      )}

      {active && active.status !== "unavailable" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xxs text-muted-foreground">
            <span className="font-medium text-foreground">{active.status}</span>
            <span>
              세션 {active.sessions.completed}/{active.sessions.total} 분석
            </span>
            {active.sessions.failed > 0 && (
              <span className="text-warning">{active.sessions.failed}건 실패</span>
            )}
            {active.sessions.ignored > 0 && (
              <span>{active.sessions.ignored}건 제외</span>
            )}
            {/* A distinct count, or nothing. Never the sum of the tree. */}
            {tree && failedSessions !== null && (
              <span>
                실패가 발견된 세션 {failedSessions}/{active.sessions.completed}
              </span>
            )}
            {tree && !tree.session_details && (
              <span>세션별 상세는 관리자에게만 보입니다</span>
            )}
            <span>{active.created_at}</span>
          </div>
          {active.errors.length > 0 && (
            <ul className="space-y-0.5 text-xxs text-warning">
              {active.errors.map((message, index) => (
                <li key={index}>{message}</li>
              ))}
            </ul>
          )}

          {running && (
            <EmptyPlot label="분석이 진행 중입니다. 상태 갱신을 눌러 확인하세요." />
          )}
          {!running && !finished && (
            <EmptyPlot label="분석이 완료되지 않아 결과가 없습니다." />
          )}

          {tree && (
            <>
              <section>
                <h4 className="mb-1 text-xxs font-medium text-muted-foreground">
                  실패 패턴 · 원인 · 권고
                </h4>
                <FailureTree tree={tree} />
              </section>
              <section>
                <h4 className="mb-1 text-xxs font-medium text-muted-foreground">
                  사용자 의도
                </h4>
                <ClusterBars
                  clusters={tree.user_intents}
                  insightId="Builtin.Insight.UserIntent"
                  tree={tree}
                  colorIndex={1}
                />
              </section>
              <section>
                <h4 className="mb-1 text-xxs font-medium text-muted-foreground">
                  실행 패턴
                </h4>
                <ClusterBars
                  clusters={tree.execution_summaries}
                  insightId="Builtin.Insight.ExecutionSummary"
                  tree={tree}
                  colorIndex={2}
                />
              </section>
            </>
          )}
        </div>
      )}
    </div>
  );
}
