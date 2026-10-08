"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  errorTone,
  formatErrorRate,
  formatLatency,
  formatMicros,
  formatRate,
  formatTokens,
  rateOf,
  shareOf,
  sortRows,
} from "@/app/insights/insightsFormat.mjs";
import type { LeaderboardRow } from "@/lib/insights";

/**
 * The columns, in the order the question is usually asked.
 *
 * `vended` marks the four that come from CloudWatch and are therefore empty
 * until a reader fetches the metered tier. They keep their place rather than
 * appearing on load: a table that grows columns is a table whose earlier
 * state looked complete.
 *
 * **`invocations` is here because `error_rate`'s denominator has to be.** The
 * table showed 턴 (our counter) beside 오류율 (CloudWatch errors ÷ CloudWatch
 * invocations) and no invocation count anywhere, so a reader had every reason to
 * read the percentage as a share of turns. It is not: a runtime invocation is not
 * a turn, the two are counted by different systems, and the ratio was fetched
 * already and thrown away. Now both denominators are on screen and a mismatch
 * between them is visible instead of being resolved by assumption.
 *
 * The cost columns are all filled on load and none is an estimate: 모델 비용 was
 * priced when each turn ended, Runtime 비용 is CloudWatch quantities at the
 * published rate. The bill is not a column — it lags two days and is a check
 * on the Runtime figure, which the 청구서 대조 widget does once for the page.
 * "요율 미등록" says why a cost cell has no number instead of showing $0; a dash
 * is a figure that could not be stated.
 */
const COLUMNS: {
  key: keyof LeaderboardRow;
  label: string;
  render: (row: LeaderboardRow) => string;
  numeric?: boolean;
  vended?: boolean;
}[] = [
  { key: "name", label: "에이전트", render: (row) => row.name },
  {
    key: "turns",
    label: "턴",
    numeric: true,
    render: (row) => row.turns.toLocaleString("ko-KR"),
  },
  {
    key: "distinct_users",
    label: "사용자",
    numeric: true,
    render: (row) => row.distinct_users.toLocaleString("ko-KR"),
  },
  {
    key: "input_tokens",
    label: "입력 토큰",
    numeric: true,
    render: (row) => formatTokens(row.input_tokens, row.unmeasured_turns),
  },
  {
    key: "output_tokens",
    label: "출력 토큰",
    numeric: true,
    render: (row) => formatTokens(row.output_tokens, row.unmeasured_turns),
  },
  {
    key: "cache_read_tokens",
    label: "캐시 읽기",
    numeric: true,
    render: (row) => formatTokens(row.cache_read_tokens, row.unmeasured_turns),
  },
  {
    key: "model_cost_micros",
    label: "모델 비용",
    numeric: true,
    render: (row) =>
      formatMicros(row.model_cost_micros, {
        unpriced: row.unpriced_turns,
        unmeasured: row.unmeasured_turns,
      }),
  },
  {
    key: "runtime_cost_micros",
    label: "Runtime 비용",
    numeric: true,
    render: (row) => formatMicros(row.runtime_cost_micros),
  },
  {
    key: "total_cost_micros",
    label: "총비용",
    numeric: true,
    render: (row) => formatMicros(row.total_cost_micros),
  },
  {
    key: "tool_calls",
    label: "툴 호출",
    numeric: true,
    render: (row) => row.tool_calls.toLocaleString("ko-KR"),
  },
  {
    key: "failed_turns",
    label: "실패",
    numeric: true,
    render: (row) => row.failed_turns.toLocaleString("ko-KR"),
  },
  {
    key: "interrupted_turns",
    label: "중단",
    numeric: true,
    render: (row) => row.interrupted_turns.toLocaleString("ko-KR"),
  },
  {
    key: "invocations",
    label: "런타임 호출",
    numeric: true,
    render: (row) =>
      row.invocations == null
        ? "—"
        : Math.round(row.invocations).toLocaleString("ko-KR"),
  },
  {
    key: "latency_p90_ms",
    label: "p90 지연",
    numeric: true,
    vended: true,
    render: (row) => formatLatency(row.latency_p90_ms ?? null),
  },
  {
    key: "error_rate",
    label: "오류율",
    numeric: true,
    vended: true,
    render: (row) => formatErrorRate(row.error_rate ?? null, row.error_basis ?? null),
  },
];

/**
 * An error rate that has earned attention, with an icon beside it.
 *
 * The icon carries the warning and the colour only reinforces it, so a reader who
 * cannot separate the warning step from the ink still sees the triangle. An
 * unmeasured rate stays a dash and borrows neither.
 *
 * `basis` is which error series the numerator holds. A rate standing on
 * `SystemErrors` alone covers half the failure modes, so it renders with a `*` and
 * the tooltip says which — the numerator used to drop the missing series in silence.
 */
function ErrorRateCell({
  rate,
  basis,
}: {
  rate: number | null;
  basis?: string[] | null;
}) {
  const tone = errorTone(rate);
  if (tone === "unknown") return <span className="text-muted-foreground">—</span>;
  const partial = Array.isArray(basis) && basis.length > 0 && basis.length < 2;

  return (
    <span
      className={cn(
        "inline-flex items-center justify-end gap-1",
        tone === "bad" && "text-destructive",
        tone === "warn" && "text-warning",
      )}
      title={
        partial
          ? `${basis?.join(", ")} 만 집계된 값입니다 (나머지 오류 계열은 이 에이전트에 없습니다)`
          : undefined
      }
    >
      {tone !== "none" && <AlertTriangle className="size-3 shrink-0" />}
      {formatErrorRate(rate, basis ?? null)}
    </span>
  );
}

/**
 * Failed turns, with their share of this agent's turns underneath.
 *
 * The same reasoning as `errorTone` and the same thresholds, but on a denominator
 * this platform counts itself — so unlike 오류율 it is filled without a CloudWatch
 * call, and unlike 오류율 its denominator is the 턴 column two cells to the left.
 * Zero gets plain ink, never green: no failures over a week is an absence, not an
 * achievement worth a verdict colour.
 */
function FailedCell({ row }: { row: LeaderboardRow }) {
  const failure = rateOf(row.failed_turns, row.turns) as { rate: number } | null;
  const tone = errorTone(failure?.rate ?? null);
  return (
    <span className="inline-flex w-full flex-col items-end">
      <span
        className={cn(
          "inline-flex items-center gap-1",
          tone === "bad" && "text-destructive",
          tone === "warn" && "text-warning",
        )}
      >
        {(tone === "bad" || tone === "warn") && (
          <AlertTriangle className="size-3 shrink-0" />
        )}
        {row.failed_turns.toLocaleString("ko-KR")}
      </span>
      {failure && row.failed_turns > 0 && (
        <span className="text-xxs text-muted-foreground">
          {formatRate(failure)}
        </span>
      )}
    </span>
  );
}

/**
 * The turn count with its share of the busiest agent underneath.
 *
 * The bar survives a re-sort: ordered by cost or latency the rows no longer
 * descend by traffic, and this is what still says "this one is most of the
 * platform's use" without the reader comparing digits.
 */
function TurnsCell({ row, max }: { row: LeaderboardRow; max: number }) {
  const share = shareOf(row.turns, max);
  return (
    <span className="inline-flex w-full flex-col items-end gap-1">
      <span>{row.turns.toLocaleString("ko-KR")}</span>
      <span className="h-[3px] w-full max-w-16 rounded-sm bg-muted">
        <span
          className="block h-full rounded-r-sm bg-[hsl(var(--chart-1))]"
          style={{ width: `${Math.max(share * 100, row.turns > 0 ? 2 : 0)}%` }}
        />
      </span>
    </span>
  );
}

export function AgentLeaderboard({
  rows,
  selected,
  onSelect,
  vendedLoaded = false,
  unclaimed = null,
  children,
}: {
  rows: LeaderboardRow[];
  selected: string | null;
  onSelect: (recordId: string | null) => void;
  /** Whether the CloudWatch tier has been fetched in this session. */
  vendedLoaded?: boolean;
  /** Runtime cost in the KPI total that belongs to no row here. */
  unclaimed?: { micros: number; runtimes: { runtime: string; runtime_cost_micros: number }[] } | null;
  children?: React.ReactNode;
}) {
  const [sortKey, setSortKey] = useState<keyof LeaderboardRow>("turns");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");

  const sorted = useMemo(
    () => sortRows(rows, sortKey, direction) as LeaderboardRow[],
    [rows, sortKey, direction],
  );

  const selectedRow = rows.find((row) => row.record_id === selected) ?? null;

  const maxTurns = useMemo(
    () => rows.reduce((max, row) => Math.max(max, row.turns), 0),
    [rows],
  );

  const toggle = (key: keyof LeaderboardRow) => {
    if (key === sortKey) {
      setDirection(direction === "desc" ? "asc" : "desc");
    } else {
      setSortKey(key);
      setDirection("desc");
    }
  };

  return (
    <div className="space-y-3">
      {/*
       * The table is the instrument, and it is the only ranking on this card now.
       * There used to be a "턴 상위 에이전트" bar chart directly above it, ranked by
       * turns — the same figure the `턴` column already draws as a bar in every row.
       * Two marks for one value cost a screenful and told the reader nothing the
       * second time, so the chart is gone and the column bar stayed: it is the one
       * that survives a re-sort by cost or latency.
       */}
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              {COLUMNS.map((column) => (
                <th
                  key={column.key as string}
                  className={cn(
                    // `whitespace-nowrap`, because the scroller is the answer to a
                    // narrow card, not wrapping: in a half-width widget "사용자"
                    // broke into three stacked characters and every row grew with
                    // it, which is worse than the horizontal scroll it was
                    // avoiding.
                    "cursor-pointer select-none whitespace-nowrap px-3 py-2 font-medium",
                    column.numeric ? "text-right" : "text-left",
                  )}
                  onClick={() => toggle(column.key)}
                >
                  <span className="inline-flex items-center gap-1">
                    {column.label}
                    {sortKey === column.key &&
                      (direction === "desc" ? (
                        <ChevronDown className="size-3" />
                      ) : (
                        <ChevronUp className="size-3" />
                      ))}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr
                key={row.record_id}
                className={cn(
                  "cursor-pointer border-b border-border/50 hover:bg-muted/40",
                  selected === row.record_id && "bg-muted/60",
                )}
                onClick={() =>
                  onSelect(selected === row.record_id ? null : row.record_id)
                }
              >
                {COLUMNS.map((column) => (
                  <td
                    key={column.key as string}
                    className={cn(
                      "whitespace-nowrap px-3 py-2 tabular-nums",
                      column.numeric ? "text-right" : "text-left",
                    )}
                  >
                    {column.vended && !vendedLoaded ? (
                      // Not "추정 불가" and not 0: nobody has asked CloudWatch
                      // yet, which is a fact about this page rather than about
                      // the agent.
                      <span className="text-muted-foreground">—</span>
                    ) : column.key === "turns" ? (
                      <TurnsCell row={row} max={maxTurns} />
                    ) : column.key === "error_rate" ? (
                      <ErrorRateCell
                        rate={row.error_rate ?? null}
                        basis={row.error_basis ?? null}
                      />
                    ) : column.key === "failed_turns" ? (
                      <FailedCell row={row} />
                    ) : (
                      column.render(row)
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Why the Runtime 비용 column does not add up to 총비용. A runtime with
          no record to sit under (an MCP-app record's runtime, a runtime a
          redeploy retired, a deleted harness's companion) still bills; live it
          was $1.22 of $37.74 and the table said nothing. The runtime names are
          the tooltip, not the sentence. */}
      {unclaimed && unclaimed.micros > 0 && (
        <p
          className="text-xxs text-muted-foreground"
          title={unclaimed.runtimes.map((entry) => entry.runtime).join(", ")}
        >
          표에 없는 런타임 {unclaimed.runtimes.length.toLocaleString("ko-KR")}개의{" "}
          {formatMicros(unclaimed.micros)} 는 총비용에만 있음
        </p>
      )}

      {!vendedLoaded ? (
        <p className="text-xxs text-muted-foreground">
          런타임 호출 · Runtime 비용 · p90 지연 · 오류율 은 CloudWatch 값입니다.
          조회할 때마다 요금이 붙는 유일한 항목이라 자동으로 읽지 않고, 위의
          &ldquo;CloudWatch&rdquo; 를 누를 때만 채웁니다.
        </p>
      ) : (
        /* The two denominators, named. 실패 is ours over 턴; 오류율 is CloudWatch's
           over 런타임 호출. They will not agree, and a reader who assumes they share
           a denominator misreads whichever one they looked at second. */
        <p className="text-xxs text-muted-foreground">
          실패 는 이 플랫폼이 기록한 턴 기준이고, 오류율 은 CloudWatch 의 런타임 호출
          기준입니다. 분모가 다르니 두 값을 직접 비교하지 마세요.
        </p>
      )}

      {/*
       * The drill-down, outside the scroll container on purpose.
       *
       * It used to be a `<td colSpan={10}>` inside the table, and that is the whole
       * reason the span timeline ran off the right edge: a cell in an auto-layout
       * table is as wide as the table, the table is wider than the card, and every
       * `flex-1`/`1fr` inside the panel resolved against *that* width. Charts drawn
       * at 1600px inside a 900px card had nowhere to go but under the horizontal
       * scrollbar. Out here the panel is as wide as the card and no wider.
       */}
      {selectedRow && (
        <section className="rounded-md border border-border">
          <header className="flex items-center gap-2 border-b border-border px-4 py-2">
            <h3 className="min-w-0 truncate text-xs font-semibold">
              {selectedRow.name}
            </h3>
            <span className="shrink-0 text-xxs text-muted-foreground">
              턴 {selectedRow.turns.toLocaleString("ko-KR")} · 사용자{" "}
              {selectedRow.distinct_users.toLocaleString("ko-KR")}
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto shrink-0"
              onClick={() => onSelect(null)}
              aria-label="닫기"
            >
              <X className="size-3.5" />
            </Button>
          </header>
          <div className="@container p-4">{children}</div>
        </section>
      )}
    </div>
  );
}
