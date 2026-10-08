"use client";

import { METRIC_COLOR } from "@/app/components/chartTheme";
import {
  dailySeries,
  formatRate,
  guardrailEventRows,
  rateOf,
  subjectLabel,
} from "@/app/insights/insightsFormat.mjs";
import type {
  AgentUsageRow,
  DailyPoint,
  GuardrailSummary,
  InsightsSources,
} from "@/lib/insights";
import {
  DailyColumns,
  EmptyPlot,
  Plot,
  PlotCell,
  PlotGrid,
  PlotStack,
  RankedBars,
  ShareBar,
  type RankedEntry,
} from "./charts";

/**
 * What the guardrail did, to whom, on what, and whether it was there at all.
 *
 * **Who / how often** is the agent list, ranked by intervention *rate* rather than
 * count — two blocks in seven turns is a pattern and two in seven hundred is a
 * footnote, and a count cannot tell them apart. **What** is the filter list: the
 * policy family alone ("content") hides the difference between a jailbreak
 * attempt (`PROMPT_ATTACK`) and a rude user (`INSULTS`), and that difference is
 * the whole of a tuning decision. **How sure** is the confidence split;
 * LOW-confidence blocks are the only evidence a filter strength is too high.
 * **Which people** is the per-user list. And **on what, exactly** is the recent
 * list at the bottom: one row per intervention with the agent, the person, the
 * filter and a link to the conversation — the trace never carries the text a
 * content filter matched, so the way to read what was said is to open the thread.
 *
 * The summary this reads is admin-only, which is what allows subjects and thread
 * ids on screen at all. Counts and labels otherwise; matched text never leaves
 * Bedrock's trace.
 *
 * Laid out as a two-column `PlotGrid`, like 구성 요소 사용, not as stacked bands.
 * The widget is full-width by default, and a ranked list stretched across a
 * full-width band is mostly empty track: the eye has to travel the whole card to
 * connect a name on the left with its figure on the right. Two-up, each list is
 * the width it needs. The six grid cells are drawn unconditionally so the grid is
 * always 3×2 — `PlotGrid` shows its border colour through an empty slot, so an
 * odd count would leave a grey block where the seventh cell would be. The recent
 * list is a table, and a table wants width, so it is its own full band below.
 */
/** "9/23 19:04" in the reader's zone; the raw string if it does not parse. */
function eventTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return `${parsed.getMonth() + 1}/${parsed.getDate()} ${parsed
    .toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

export function GuardrailPanel({
  guardrail,
  sources,
  agents = [],
  daily = [],
  partialDay,
  subjects,
}: {
  guardrail: GuardrailSummary | null;
  sources?: InsightsSources;
  /** For names: the guardrail rollup is keyed by record id. */
  agents?: AgentUsageRow[];
  daily?: DailyPoint[];
  partialDay?: string | null;
  /** sub -> email, for the people the rollup and the event rows name. */
  subjects?: Record<string, string>;
}) {
  if (sources?.guardrail === false) {
    return (
      <PlotStack>
        <PlotCell>
          <Plot title="가드레일 개입" hint="">
            <EmptyPlot label="가드레일 미설정 또는 집계 불가" />
          </Plot>
        </PlotCell>
      </PlotStack>
    );
  }

  const totals = guardrail?.totals;
  const interventions = totals?.interventions ?? 0;

  if (!guardrail || interventions === 0) {
    return (
      <PlotStack>
        <PlotCell>
          <Plot
            title="가드레일 개입"
            hint={
              totals && totals.scanned_turns > 0
                ? `스캔한 턴 ${totals.scanned_turns.toLocaleString("ko-KR")}회, 개입 없음`
                : ""
            }
          >
            <EmptyPlot label="이 기간 가드레일 개입이 없습니다." />
          </Plot>
        </PlotCell>
      </PlotStack>
    );
  }

  const actionSegments = [
    { name: "입력 차단", value: totals!.blocked_input },
    { name: "출력 차단", value: totals!.blocked_output },
    { name: "입력 마스킹", value: totals!.anonymized_input },
    { name: "출력 마스킹", value: totals!.anonymized_output },
  ]
    .filter((segment) => segment.value > 0)
    .map((segment) => ({ ...segment, display: segment.value.toLocaleString("ko-KR") }));

  // The filter list is the "what". Sorted by count; the label is Bedrock's own
  // filter name so it can be matched against the guardrail's configuration.
  const filterEntries: RankedEntry[] = Object.entries(totals!.by_filter ?? {})
    .map(([filter, count]) => ({
      name: filter,
      value: count,
      display: count.toLocaleString("ko-KR"),
      id: filter,
    }))
    .sort((a, b) => b.value - a.value);

  // HIGH → MEDIUM → LOW, fixed, so the bar reads left-to-right as "sure → unsure"
  // regardless of which level happens to be largest this window.
  const confidenceOrder = ["HIGH", "MEDIUM", "LOW"];
  const confidenceSegments = confidenceOrder
    .map((level) => ({
      name: { HIGH: "확신 높음", MEDIUM: "확신 중간", LOW: "확신 낮음" }[level] ?? level,
      value: totals!.by_confidence?.[level] ?? 0,
    }))
    .filter((segment) => segment.value > 0)
    .map((segment) => ({ ...segment, display: segment.value.toLocaleString("ko-KR") }));
  const lowConfidence = totals!.by_confidence?.LOW ?? 0;

  // Agents by rate, with the count as the visible figure: the bar length answers
  // "how often", the number answers "how many", and neither is hidden behind the
  // other.
  const nameOf = (recordId: string) =>
    agents.find((agent) => agent.record_id === recordId)?.name ?? recordId;
  const agentEntries: RankedEntry[] = Object.entries(guardrail.by_agent)
    .filter(([, counters]) => counters.interventions > 0)
    .map(([recordId, counters]) => {
      const rate = counters.intervention_rate ?? null;
      const turns = agents.find((agent) => agent.record_id === recordId)?.turns;
      return {
        name: nameOf(recordId),
        value: rate ?? 0,
        display: counters.interventions.toLocaleString("ko-KR"),
        meta:
          rate != null && turns != null
            ? `/ ${turns.toLocaleString("ko-KR")}턴 · ${formatRate(rateOf(counters.interventions, turns))}`
            : undefined,
        id: recordId,
      };
    })
    .sort((a, b) => b.value - a.value);

  // People, by count. Rate per person is not meaningful here — the summary
  // does not carry per-user turns — so the bar is the count and says so.
  const userEntries: RankedEntry[] = Object.entries(guardrail.by_user ?? {})
    .filter(([, counters]) => counters.interventions > 0)
    .map(([sub, counters]) => ({
      name: subjectLabel(sub, subjects),
      value: counters.interventions,
      display: counters.interventions.toLocaleString("ko-KR"),
      meta: `/ 스캔 ${counters.scanned_turns.toLocaleString("ko-KR")}턴`,
      id: sub,
    }))
    .sort((a, b) => b.value - a.value);

  const recentRows = guardrailEventRows(guardrail.recent ?? [], agents, subjects) as Array<{
    key: string;
    at: string;
    agent: string;
    user: string;
    filters: string;
    action: string;
    threadId: string | null;
  }>;

  const series = dailySeries(daily, partialDay) as Array<Record<string, unknown>>;
  const totalRate = totals!.intervention_rate;

  return (
    <PlotStack>
      {/* Row 1: the two part-to-whole bars. Row 2: by agent and by filter. Row 3: by
          person and by day. Cells are unconditional, see the component note. */}
      <PlotGrid>
        <PlotCell>
          <Plot
            title="실행 유형별 분포"
            hint={
              totalRate != null
                ? `개입 ${interventions.toLocaleString("ko-KR")}회 · 전체 턴의 ${formatRate({ rate: totalRate })} · 스캔한 턴 ${totals!.scanned_turns.toLocaleString("ko-KR")}회`
                : `개입 ${interventions.toLocaleString("ko-KR")}회`
            }
          >
            <ShareBar segments={actionSegments} />
          </Plot>
        </PlotCell>

        <PlotCell>
          <Plot
            title="확신도 분포"
            hint={
              lowConfidence > 0
                ? `확신 낮음 ${lowConfidence.toLocaleString("ko-KR")}건은 오탐일 수 있습니다. 해당 필터 강도를 낮출지 검토하세요.`
                : "콘텐츠 필터가 판정에 붙인 확신도입니다. 낮은 확신의 차단이 쌓이면 필터 강도가 과한 신호입니다."
            }
          >
            {confidenceSegments.length > 0 ? (
              <ShareBar segments={confidenceSegments} />
            ) : (
              <EmptyPlot label="확신도를 보고하는 콘텐츠 필터 개입이 없습니다." />
            )}
          </Plot>
        </PlotCell>

        <PlotCell>
          <Plot
            title="필터별 개입"
            hint="어떤 필터가 걸렸는지입니다. PROMPT_ATTACK 은 프롬프트 공격 시도, INSULTS·HATE 등은 콘텐츠 필터입니다."
          >
            <RankedBars
              entries={filterEntries}
              colorIndex={METRIC_COLOR.failed_turns}
              empty="필터 종류가 기록된 개입이 없습니다."
            />
          </Plot>
        </PlotCell>

        <PlotCell>
          <Plot
            title="에이전트별 개입률"
            hint="막대는 개입 횟수를 턴 수로 나눈 비율, 숫자는 개입 횟수입니다."
          >
            {/* An intervention count is the "something was flagged/stopped" nature,
                so it shares the alert hue with failed turns rather than the neutral
                turns blue. */}
            <RankedBars
              entries={agentEntries}
              colorIndex={METRIC_COLOR.failed_turns}
              empty="에이전트 개입 기록이 없습니다."
            />
          </Plot>
        </PlotCell>

        <PlotCell>
          <Plot
            title="사용자별 개입"
            hint="개입된 턴의 사용자"
          >
            <RankedBars
              entries={userEntries}
              colorIndex={METRIC_COLOR.failed_turns}
              empty="사용자 개입 기록이 없습니다."
            />
          </Plot>
        </PlotCell>

        <PlotCell>
          <Plot title="일별 개입" hint="하루에 가드레일이 개입한 횟수입니다.">
            {series.length > 0 ? (
              <DailyColumns
                data={series}
                dataKey="guardrail_interventions"
                name="개입"
                colorIndex={METRIC_COLOR.failed_turns}
              />
            ) : (
              <EmptyPlot label="일별 기록이 없습니다." />
            )}
          </Plot>
        </PlotCell>
      </PlotGrid>

      <PlotCell>
        <Plot
          title="최근 개입"
          hint="개입 한 건이 한 줄입니다. 걸린 문장은 트레이스에 없으므로 대화를 열어 확인합니다."
        >
          {recentRows.length === 0 ? (
            <EmptyPlot label="개입 이벤트가 아직 없습니다. 이벤트 기록은 배포 시점부터 쌓입니다." />
          ) : (
            <div className="max-h-64 overflow-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">시각</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">에이전트</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">사용자</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">필터</th>
                    <th className="whitespace-nowrap px-2 py-1 text-left font-medium">조치</th>
                    <th className="whitespace-nowrap px-2 py-1 text-right font-medium">대화</th>
                  </tr>
                </thead>
                <tbody>
                  {recentRows.map((row) => (
                    <tr key={row.key} className="border-b border-border/50 last:border-0">
                      <td className="whitespace-nowrap px-2 py-1 tabular-nums">{eventTime(row.at)}</td>
                      <td className="max-w-40 truncate px-2 py-1" title={row.agent}>
                        {row.agent}
                      </td>
                      <td className="whitespace-nowrap px-2 py-1 font-mono text-xxs">{row.user}</td>
                      <td className="px-2 py-1 font-mono text-xxs">{row.filters}</td>
                      <td className="whitespace-nowrap px-2 py-1">{row.action}</td>
                      <td className="whitespace-nowrap px-2 py-1 text-right">
                        {row.threadId ? (
                          <a
                            className="text-info underline"
                            href={`/?threadId=${encodeURIComponent(row.threadId)}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            열기
                          </a>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Plot>
      </PlotCell>
    </PlotStack>
  );
}
