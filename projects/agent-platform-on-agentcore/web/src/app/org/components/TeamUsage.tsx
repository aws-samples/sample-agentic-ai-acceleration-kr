"use client";

import { formatMicros } from "@/app/insights/insightsFormat.mjs";
import { alertSeries } from "@/app/insights/teamInsights.mjs";
import { ThresholdColumns } from "@/app/insights/components/charts";
import { useTeamInsights } from "@/app/insights/components/useTeamInsights";
import { shortTool } from "../orgTable.mjs";

const DAYS = 30;
const usd = (v: number) => `$${v.toFixed(v >= 1 ? 2 : 4)}`;

/**
 * One team's last 30 days in the team sheet: the figures the table row only
 * hints at, from the same `/api/insights/teams` read the Insights team widget
 * draws, so the two never disagree.
 */
export function TeamUsage({ team }: { team: string }) {
  const result = useTeamInsights(DAYS);
  if (result.state !== "ready") {
    const label = result.state === "loading" ? "불러오는 중…" : result.state === "forbidden" ? "관리자만 볼 수 있습니다" : "불러오지 못했습니다";
    return <p className="mt-1 text-xxs text-muted-foreground">{label}</p>;
  }
  const row = result.data.teams.find((t) => t.team === team);
  if (!row || row.turns === 0) return <p className="mt-1 text-xxs text-muted-foreground">이 기간 사용 없음</p>;

  const failRate = row.turns > 0 ? Math.round((row.failed_turns / row.turns) * 100) : 0;
  const denied = Object.entries(row.denied_tools).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const stats: Array<[string, string, string?]> = [
    ["모델 비용", formatMicros(row.model_cost_micros, { unpriced: row.unpriced_turns }), row.unpriced_turns > 0 ? `가격 미등록 ${row.unpriced_turns}턴 제외` : undefined],
    ["턴", row.turns.toLocaleString("ko-KR"), `실패 ${failRate}%`],
    ["정책 거부", row.policy_denials.toLocaleString("ko-KR")],
    ["경고 초과", row.days_over_alert === null ? "경고 없음" : `${row.days_over_alert}일`, `최대 ${formatMicros(row.max_day_cost_micros)}/일`],
  ];

  return (
    <div className="mt-2 grid gap-3">
      <div className="grid grid-cols-4 gap-2">
        {stats.map(([label, value, sub]) => (
          <div key={label} className="rounded-md border px-2 py-1.5">
            <div className="text-xxs text-muted-foreground">{label}</div>
            <div className="text-sm font-semibold tabular-nums">{value}</div>
            {sub && <div className="text-xxs text-muted-foreground">{sub}</div>}
          </div>
        ))}
      </div>
      <div>
        <div className="text-xxs text-muted-foreground">
          일별 모델 비용{row.daily_cost_alert_usd !== null && ` · 점선은 일일 경고 ${usd(row.daily_cost_alert_usd)}`}
        </div>
        <ThresholdColumns data={alertSeries(row, result.data.partial_day ?? null)} threshold={row.daily_cost_alert_usd} format={usd} />
      </div>
      {denied.length > 0 && (
        <div className="text-xxs">
          <span className="text-muted-foreground">거부된 툴 · </span>
          {denied.map(([tool, n]) => <span key={tool} title={tool} className="mr-2">{shortTool(tool)} {n}</span>)}
        </div>
      )}
    </div>
  );
}
