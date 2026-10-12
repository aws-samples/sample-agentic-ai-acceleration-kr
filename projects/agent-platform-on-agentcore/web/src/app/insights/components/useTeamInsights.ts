"use client";

import { useEffect, useState } from "react";
import { fetchTeamInsights, InsightsApiError, type TeamInsights } from "@/lib/insights";

/**
 * `/api/insights/teams` for the two widgets that draw it (팀별 사용량 and
 * 게이트웨이 정책). They mount together, so an in-flight request is shared for
 * a few seconds rather than read twice — each read is a member-count call per
 * team against Cognito. Short on purpose: a refresh a minute later must refetch.
 */
const SHARE_MS = 5_000;
const inflight = new Map<number, { at: number; promise: Promise<TeamInsights> }>();

function load(days: number): Promise<TeamInsights> {
  const hit = inflight.get(days);
  if (hit && Date.now() - hit.at < SHARE_MS) return hit.promise;
  const promise = fetchTeamInsights(days);
  inflight.set(days, { at: Date.now(), promise });
  // A failed read must not be served to the next mount.
  promise.catch(() => inflight.delete(days));
  return promise;
}

export type TeamInsightsState =
  | { state: "loading" }
  | { state: "forbidden" }
  | { state: "error"; message: string }
  | { state: "ready"; data: TeamInsights };

export function useTeamInsights(days: number): TeamInsightsState {
  const [result, setResult] = useState<TeamInsightsState>({ state: "loading" });
  useEffect(() => {
    let live = true;
    setResult({ state: "loading" });
    load(days)
      .then((data) => live && setResult({ state: "ready", data }))
      .catch((e) => {
        if (!live) return;
        if (e instanceof InsightsApiError && e.status === 403) setResult({ state: "forbidden" });
        else setResult({ state: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => {
      live = false;
    };
  }, [days]);
  return result;
}
