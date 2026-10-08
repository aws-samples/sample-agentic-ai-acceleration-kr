"use client";

import { useEffect, useState } from "react";

import {
  cacheHitRate,
  formatMicros,
  formatRate,
  formatTokens,
  rateOf,
  shareOf,
  subjectLabel,
  topNWithOther,
} from "@/app/insights/insightsFormat.mjs";
import {
  fetchUserLeaderboard,
  InsightsApiError,
  type UserLeaderboard,
  type UserUsageRow,
} from "@/lib/insights";
import { METRIC_COLOR } from "@/app/components/chartTheme";
import { DailyColumns, EmptyPlot, Plot, PlotCell, PlotGrid, RankedBars, type RankedEntry } from "./charts";

/**
 * Usage per person — the chargeback question, finally rendered.
 *
 * The counters behind this have been written on every turn since the feature
 * shipped: `usage_service.record_turn` stores one item per (day, user), and
 * `/api/insights/me` has been serving them the whole time. Nothing on the page ever
 * read either. "Which team is spending this" is the first thing an enterprise
 * platform owner asks, and the answer was already in the table.
 *
 * **Its own request, and not on the poll.** Admin-only, so a plain user's 403 must
 * not blank the widgets beside it — and a 403 here is the correct answer rather
 * than a failure, so it renders as "관리자만" instead of as an error.
 *
 * Cost per person is the ledger's own sum: each turn was priced by the model it
 * ran on when it ended, and the figure rode onto the user's day item. Runtime
 * cost per person needs per-session USAGE_LOGS; until those are on, the column
 * says so rather than showing zero.
 */
export function UserPanel({ days }: { days: number }) {
  const [data, setData] = useState<UserLeaderboard | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">(
    "loading",
  );
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setState("loading");
    fetchUserLeaderboard(days)
      .then((value) => {
        if (!live) return;
        setData(value);
        setState("ready");
      })
      .catch((error) => {
        if (!live) return;
        if (error instanceof InsightsApiError && error.status === 403) {
          setState("forbidden");
          return;
        }
        setMessage(error instanceof Error ? error.message : String(error));
        setState("error");
      });
    return () => {
      live = false;
    };
  }, [days]);

  if (state === "forbidden") {
    return (
      <PlotCell>
        <EmptyPlot label="사용자별 사용량은 관리자만 볼 수 있습니다." />
      </PlotCell>
    );
  }
  if (state === "error") {
    return (
      <PlotCell>
        <EmptyPlot label={message ?? "사용자별 사용량을 읽지 못했습니다."} />
      </PlotCell>
    );
  }
  if (state === "loading" || !data) {
    return (
      <PlotCell>
        <EmptyPlot label="사용자별 사용량을 읽고 있습니다…" />
      </PlotCell>
    );
  }
  if (data.users.length === 0) {
    return (
      <PlotCell>
        <EmptyPlot label="이 기간에 기록된 사용자가 없습니다." />
      </PlotCell>
    );
  }

  // The email's local part when the server could name the sub, else the sub.
  const label = (sub: string) => subjectLabel(sub, data.subjects);

  const byTurns = topNWithOther(
    data.users.map((user: UserUsageRow) => ({
      name: label(user.sub),
      value: user.turns,
    })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: `${entry.value.toLocaleString("ko-KR")}턴`,
  })) as RankedEntry[];

  // Prompt tokens, all tiers: the count that tracks spend rather than activity. A
  // reader comparing this list against the turn list above is looking for the person
  // whose few conversations are expensive, which is the whole reason both are drawn.
  const byTokens = topNWithOther(
    data.users.map((user: UserUsageRow) => ({
      name: label(user.sub),
      value:
        user.input_tokens +
        user.output_tokens +
        user.cache_read_tokens +
        user.cache_write_tokens,
    })),
    8,
  ).map((entry: RankedEntry) => ({
    ...entry,
    display: entry.value.toLocaleString("ko-KR"),
  })) as RankedEntry[];

  const maxTurns = data.users.reduce(
    (max: number, user: UserUsageRow) => Math.max(max, user.turns),
    0,
  );

  // Two things the totals cannot say: which agents a person actually uses, and
  // how many people were here each day. The first is the "what does this team
  // use" question; the second separates one heavy user from ten light ones.
  const dailyUsers = (data.daily_active_users ?? []).map((point) => ({
    date: point.date,
    label: point.date.slice(5),
    users: point.users,
  }));
  const agentsOf = (user: UserUsageRow) => {
    const agents = user.agents ?? [];
    if (agents.length === 0) return "—";
    const [top, ...rest] = agents;
    return rest.length > 0 ? `${top.name} +${rest.length}` : top.name;
  };

  return (
    <>
      {dailyUsers.length > 1 && (
        <PlotCell>
          <Plot
            title="일별 활성 사용자"
            hint="그날 한 턴 이상 대화한 사람 수"
          >
            <DailyColumns data={dailyUsers} dataKey="users" name="사용자" colorIndex={METRIC_COLOR.turns} />
          </Plot>
        </PlotCell>
      )}
      <PlotGrid>
        <PlotCell>
          <Plot title="사용자별 턴">
            <RankedBars entries={byTurns} colorIndex={METRIC_COLOR.turns} />
          </Plot>
        </PlotCell>
        <PlotCell>
          <Plot title="사용자별 토큰" hint="입력 + 출력 + 캐시">
            {/* Tokens, not turns — the token-family hue, so this list is not mistaken
                for another turn count sitting beside 사용자별 턴. */}
            <RankedBars entries={byTokens} colorIndex={METRIC_COLOR.input_tokens} />
          </Plot>
        </PlotCell>
      </PlotGrid>

      <PlotCell>
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                  사용자
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-left font-medium">
                  주로 쓴 에이전트
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  활동일
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  턴
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  스레드
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  입력
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  출력
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  캐시 적중
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  툴 호출
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  실패
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  중단
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  가드레일
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  모델 비용
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  Runtime 비용
                  {!data.sources.usage_logs && (
                    <span className="ml-1 font-normal text-xxs text-muted-foreground">세션 로그 미활성</span>
                  )}
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                  총비용
                </th>
              </tr>
            </thead>
            <tbody>
              {data.users.map((user: UserUsageRow) => {
                const cache = cacheHitRate(user) as { rate: number } | null;
                const failure = rateOf(user.failed_turns, user.turns) as {
                  rate: number;
                } | null;
                const guarded = rateOf(user.guardrail_interventions ?? 0, user.turns) as {
                  rate: number;
                } | null;
                return (
                  <tr key={user.sub} className="border-b border-border/50 last:border-0">
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-xxs">
                      {label(user.sub)}
                    </td>
                    {/* The most-used agent by turns, with how many others; the
                        full list is the tooltip. */}
                    <td
                      className="whitespace-nowrap px-3 py-2 text-xxs"
                      title={(user.agents ?? [])
                        .map((agent) => `${agent.name} ${agent.turns.toLocaleString("ko-KR")}턴`)
                        .join(", ")}
                    >
                      {agentsOf(user)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {(user.active_days ?? 0).toLocaleString("ko-KR")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      <span className="inline-flex w-full flex-col items-end gap-1">
                        <span>{user.turns.toLocaleString("ko-KR")}</span>
                        <span className="h-[3px] w-full max-w-16 rounded-sm bg-muted">
                          <span
                            className="block h-full rounded-r-sm bg-[hsl(var(--chart-1))]"
                            style={{
                              width: `${Math.max(
                                shareOf(user.turns, maxTurns) * 100,
                                user.turns > 0 ? 2 : 0,
                              )}%`,
                            }}
                          />
                        </span>
                      </span>
                    </td>
                    {/* Conversations started; against 턴 it reads as depth — one
                        person asks seven things in two threads, another asks
                        seven things in seven. */}
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {(user.threads_started ?? 0).toLocaleString("ko-KR")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatTokens(user.input_tokens, user.unmeasured_turns)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatTokens(user.output_tokens, user.unmeasured_turns)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatRate(cache)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {(user.tool_calls ?? 0).toLocaleString("ko-KR")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {user.failed_turns > 0 && failure
                        ? `${user.failed_turns.toLocaleString("ko-KR")} · ${formatRate(failure)}`
                        : user.failed_turns.toLocaleString("ko-KR")}
                    </td>
                    {/* Answers this person walked away from mid-stream. A failure
                        is the platform's ending; an interruption is theirs — many
                        of these on one row is a slow agent, not a broken one. */}
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {(user.interrupted_turns ?? 0).toLocaleString("ko-KR")}
                    </td>
                    {/* Interventions with their rate, same form as 실패: who keeps
                        tripping the guardrail is the per-person half of the
                        governance question the agent list cannot answer. */}
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {(user.guardrail_interventions ?? 0) > 0 && guarded
                        ? `${(user.guardrail_interventions ?? 0).toLocaleString("ko-KR")} · ${formatRate(guarded)}`
                        : (user.guardrail_interventions ?? 0).toLocaleString("ko-KR")}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      <span className="inline-flex w-full flex-col items-end gap-1">
                        <span>
                          {formatMicros(user.model_cost_micros, {
                            unpriced: user.unpriced_turns,
                            unmeasured: user.unmeasured_turns,
                          })}
                        </span>
                        {user.unpriced_turns > 0 && user.model_cost_micros !== null && (
                          <span className="text-xxs text-muted-foreground">
                            요율 미등록 {user.unpriced_turns}턴 제외
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatMicros(user.runtime_cost_micros)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {formatMicros(user.total_cost_micros)}
                    </td>
                  </tr>
                );
              })}
              <tr className="border-t border-border bg-muted/30 font-medium">
                <td className="whitespace-nowrap px-3 py-2 text-left">
                  합계
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-left">—</td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">—</td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {data.totals.turns.toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {(data.totals.threads_started ?? 0).toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatTokens(data.totals.input_tokens)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatTokens(data.totals.output_tokens)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  —
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {(data.totals.tool_calls ?? 0).toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {data.totals.failed_turns.toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {(data.totals.interrupted_turns ?? 0).toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {(data.totals.guardrail_interventions ?? 0).toLocaleString("ko-KR")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatMicros(data.totals.model_cost_micros, { unpriced: data.totals.unpriced_turns })}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatMicros(data.totals.runtime_cost_micros)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatMicros(
                    data.totals.model_cost_micros !== null && data.totals.runtime_cost_micros !== null
                      ? data.totals.model_cost_micros + data.totals.runtime_cost_micros
                      : null,
                  )}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xxs text-muted-foreground">
          사용자 {data.totals.users.toLocaleString("ko-KR")}명 · 턴{" "}
          {data.totals.turns.toLocaleString("ko-KR")}
          {/* The column is a person's conversations only (per-session logs);
              keep-warm and pre-log runtime belong to nobody, so this sum is
              smaller than the platform total by design. One clause, not a
              paragraph — the earlier version quoted dates and dollar figures. */}
          {data.sources.usage_logs && " · Runtime 비용은 대화 세션분만"}
          {/* Endings the KPI counted before the per-user ledger recorded them;
              they sit in no row. */}
          {((data.totals.interrupted_turns_unattributed ?? 0) > 0 ||
            (data.totals.failed_turns_unattributed ?? 0) > 0) && (
            <>
              {" "}
              · 사용자를 모르는 중단 {(data.totals.interrupted_turns_unattributed ?? 0).toLocaleString("ko-KR")}건
              {(data.totals.failed_turns_unattributed ?? 0) > 0 &&
                ` · 실패 ${(data.totals.failed_turns_unattributed ?? 0).toLocaleString("ko-KR")}건`}
              은 표에 없음
            </>
          )}
        </p>
      </PlotCell>
    </>
  );
}
