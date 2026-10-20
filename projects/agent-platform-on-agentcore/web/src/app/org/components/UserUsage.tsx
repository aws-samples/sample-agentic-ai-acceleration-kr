"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { formatMicros, shortDate } from "@/app/insights/insightsFormat.mjs";
import { ThresholdColumns } from "@/app/insights/components/charts";
import { fetchUserInsights, type UserInsights } from "@/lib/insights";

const DAYS = 30;
const usd = (v: number) => `$${v.toFixed(v >= 1 ? 2 : 4)}`;

/** Every day from start to end inclusive, `YYYY-MM-DD`. */
function windowDays(start: string, end: string): string[] {
  const out: string[] = [];
  for (let d = new Date(`${start}T00:00:00Z`); d.toISOString().slice(0, 10) <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

/**
 * One person's last 30 days in the user sheet, from `/api/insights/users/{sub}`:
 * the Insights leaderboard's row for them, read without everyone else's rows.
 */
export function UserUsage({ sub }: { sub: string }) {
  const [data, setData] = useState<UserInsights | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    setData(null);
    setFailed(false);
    fetchUserInsights(sub, DAYS).then((d) => live && setData(d)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, [sub]);

  if (!data) return <p className="mt-1 text-xxs text-muted-foreground">{failed ? "불러오지 못했습니다" : "불러오는 중…"}</p>;
  const row = data.user;
  if (!row || row.turns === 0) return <p className="mt-1 text-xxs text-muted-foreground">이 기간 사용 없음</p>;

  const failRate = Math.round((row.failed_turns / row.turns) * 100);
  const stats: Array<[string, string, string?]> = [
    ["모델 비용", formatMicros(row.model_cost_micros, { unpriced: row.unpriced_turns }), row.unpriced_turns > 0 ? `가격 미등록 ${row.unpriced_turns}턴 제외` : undefined],
    ["턴", row.turns.toLocaleString("ko-KR"), `실패 ${failRate}%`],
    ["대화", (row.threads_started ?? 0).toLocaleString("ko-KR")],
    ["활동일", `${row.active_days ?? 0}일`],
  ];
  // The person's days are sparse; an idle day is a zero column, not a gap.
  const byDay = new Map((row.daily ?? []).map((p) => [p.date, p.model_cost_micros]));
  const series = windowDays(data.start_date, data.end_date).map((date) => ({
    date,
    label: shortDate(date) + (date === data.partial_day ? "*" : ""),
    cost: (byDay.get(date) ?? 0) / 1_000_000,
    over: false,
  }));

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
        <div className="text-xxs text-muted-foreground">일별 모델 비용</div>
        <ThresholdColumns data={series} threshold={null} format={usd} />
      </div>
      {(row.agents ?? []).length > 0 && (
        <div className="text-xxs">
          <span className="text-muted-foreground">많이 쓴 에이전트 · </span>
          {(row.agents ?? []).map((a) => (
            <Link key={a.record_id} href={`/registry?record=${encodeURIComponent(a.record_id)}`} className="mr-2 hover:underline">
              {a.name} {a.turns}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
