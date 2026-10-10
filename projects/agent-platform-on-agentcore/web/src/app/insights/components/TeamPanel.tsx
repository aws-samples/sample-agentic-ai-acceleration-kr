"use client";

import { useEffect, useState } from "react";
import { formatMicros, formatTokens } from "@/app/insights/insightsFormat.mjs";
import { fetchTeamInsights, InsightsApiError, type TeamInsights } from "@/lib/insights";
import { EmptyPlot, PlotCell, PlotGrid } from "./charts";

/**
 * Spend and policy denials per team — the chargeback table an enterprise owner
 * asks for, plus proof that the gateway policy is doing something.
 *
 * Rows come from the ledger (turns attributed by the caller's groups). The
 * "게이트웨이 결정" line comes from the policy engine's own CloudWatch metrics,
 * which know nothing about teams; when it disagrees with the rows the gap is the
 * unattributed row, never a guess.
 */
export function TeamPanel({ days }: { days: number }) {
  const [data, setData] = useState<TeamInsights | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setState("loading");
    fetchTeamInsights(days)
      .then((v) => {
        if (!live) return;
        setData(v);
        setState("ready");
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof InsightsApiError && e.status === 403) {
          setState("forbidden");
          return;
        }
        setMessage(e instanceof Error ? e.message : String(e));
        setState("error");
      });
    return () => {
      live = false;
    };
  }, [days]);

  if (state === "forbidden") return <EmptyPlot label="관리자만 볼 수 있습니다." />;
  if (state === "error") return <EmptyPlot label={`불러오지 못했습니다: ${message}`} />;
  if (state === "loading" || !data) return <EmptyPlot label="불러오는 중…" />;
  if (data.teams.length === 0) {
    return <EmptyPlot label="이 배포에는 팀이 없습니다. terraform 의 teams 변수를 확인하세요." />;
  }

  const d = data.gateway_decisions;
  return (
    <PlotGrid>
      <PlotCell>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1">팀</th>
              <th>턴</th>
              <th>토큰(입력/출력)</th>
              <th>모델 비용</th>
              <th>정책 거부</th>
              <th>거부된 툴</th>
            </tr>
          </thead>
          <tbody>
            {data.teams.map((r) => (
              <tr key={r.team} className="border-t">
                <td className="py-1 font-medium">{r.label || r.team}</td>
                <td>{r.turns}</td>
                <td>
                  {formatTokens(r.input_tokens)} / {formatTokens(r.output_tokens)}
                </td>
                <td>{r.priced_turns > 0 ? formatMicros(r.model_cost_micros) : "—"}</td>
                <td>{r.policy_denials}</td>
                <td className="text-muted-foreground">
                  {Object.entries(r.denied_tools)
                    .map(([t, n]) => `${t.split("___").pop()} ${n}`)
                    .join(", ") || "—"}
                </td>
              </tr>
            ))}
            <tr className="border-t text-muted-foreground">
              <td className="py-1">미귀속</td>
              <td>{data.unattributed.turns}</td>
              <td>—</td>
              <td>{formatMicros(data.unattributed.model_cost_micros)}</td>
              <td>{data.unattributed.policy_denials}</td>
              <td>—</td>
            </tr>
          </tbody>
        </table>
        <p className="mt-2 text-xs text-muted-foreground">
          게이트웨이 결정(CloudWatch 지표):{" "}
          {d.by_mode && Object.keys(d.by_mode).length > 0
            ? Object.entries(d.by_mode)
                .map(([mode, c]) =>
                  mode === "ENFORCE"
                    ? `ENFORCE 허용 ${c.allow} · 차단 ${c.deny}`
                    : `${mode} 허용 ${c.allow} · 거부 판정 ${c.deny}(통과)`,
                )
                .join(" / ")
            : `허용 ${d.allow} · 거부 ${d.deny}`}
          {!data.sources.policy_metrics && " · 지표 미수집"}
        </p>
      </PlotCell>
    </PlotGrid>
  );
}
