"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw, User } from "lucide-react";

import { MUTED_MARK, SERIES_COLORS } from "@/app/components/chartTheme";
import {
  fetchRecordThreads,
  fetchThreadTraces,
  type RecordThread,
  type ThreadTraces,
} from "@/lib/insights";
import { formatThreadTime } from "@/app/insights/threadRows.mjs";
import { subjectLabel } from "@/app/insights/insightsFormat.mjs";
import {
  SPAN_KINDS,
  formatSeconds,
  traceAxisTicks,
  traceBars,
} from "@/app/insights/turnTrace.mjs";
import { EmptyPlot } from "./charts";

/**
 * Hue per span kind, assigned by position in `SPAN_KINDS` and never re-packed.
 *
 * The timeline was one colour for every bar, which spent the only channel that
 * could have said what each span *was* — a reader saw sixteen identical marks and
 * had to read sixteen names to learn that four of them were model calls. Colour
 * follows the kind, so a thread with no tool calls does not hand the tool colour to
 * something else.
 */
const KIND_LABEL = new Map<string, string>(
  (SPAN_KINDS as Array<{ key: string; label: string }>).map((kind) => [
    kind.key,
    kind.label,
  ]),
);
const KIND_COLOR = new Map<string, string>(
  (SPAN_KINDS as Array<{ key: string }>).map((kind, index) => [
    kind.key,
    SERIES_COLORS[index % SERIES_COLORS.length],
  ]),
);

/**
 * The row geometry, in one place because the axis and the bars must share it.
 *
 * A grid rather than a flex row, and the middle track is `minmax(0, 1fr)`: that is
 * what makes overflow structurally impossible here. This panel used to be a flex
 * row of `w-32` / `flex-1` / `w-16`, which is fine on its own — but it was mounted
 * inside the leaderboard's `overflow-x-auto` table cell, so `1fr` resolved against
 * the *table's* natural width and the timeline ran past the card. The panel has
 * since moved out of the table; the explicit zero minimum keeps it out.
 */
const ROW =
  "grid grid-cols-[9rem_minmax(0,1fr)_3.5rem] items-center gap-2 @2xl:grid-cols-[16rem_minmax(0,1fr)_4rem]";

interface Bar {
  name: string;
  depth: number;
  kind: string;
  durationMs: number | null;
  startOffsetMs: number | null;
  offsetPercent: number;
  widthPercent: number;
}

/**
 * The timeline's own axis.
 *
 * Without it the bars were percentages of an unstated total: a reader could see
 * that one span was wider than another and had no way to know the panel spanned
 * four seconds rather than four minutes. The ticks are drawn from the same
 * `totalMs` the bars were divided by, so they cannot disagree.
 */
function TimelineAxis({ totalMs }: { totalMs: number }) {
  const ticks = traceAxisTicks(totalMs) as Array<{
    percent: number;
    label: string;
  }>;
  if (ticks.length === 0) return null;

  return (
    <div className={ROW}>
      <span className="text-xxs uppercase tracking-wider text-muted-foreground">
        스팬
      </span>
      <span className="relative block h-4 min-w-0">
        {ticks.map((tick) => (
          <span
            key={tick.percent}
            className="absolute top-0 whitespace-nowrap text-xxs tabular-nums text-muted-foreground"
            style={{
              left: `${tick.percent}%`,
              // The end labels are pulled inside the track rather than centred on
              // their tick, which would hang them over the neighbouring columns.
              transform:
                tick.percent === 0
                  ? "none"
                  : tick.percent === 100
                    ? "translateX(-100%)"
                    : "translateX(-50%)",
            }}
          >
            {tick.label}
          </span>
        ))}
      </span>
      <span className="text-right text-xxs uppercase tracking-wider text-muted-foreground">
        소요
      </span>
    </div>
  );
}

/**
 * What the colours mean — one key for the whole list, not per row.
 *
 * It used to render inside every opened row, listing only that thread's kinds with
 * their counts, so a reader who opened three rows read the same key three times. The
 * kinds are a fixed taxonomy with fixed colours, so a single reference key above the
 * list serves every timeline. The per-thread counts went with it — they were a free
 * extra, not the legend's job, and the total span count now sits with each timeline.
 *
 * Every kind is listed, present on screen or not, because a shared key cannot know
 * which rows are open; that is the trade for stating it once.
 */
function SpanKindLegend() {
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {(SPAN_KINDS as Array<{ key: string; label: string }>).map((kind) => (
        <li key={kind.key} className="flex items-center gap-1.5 text-xxs">
          {/* The swatch carries identity and the label stays in text ink — a light
              categorical hue is not readable as type at this size. */}
          <span
            className="size-2 shrink-0 rounded-sm"
            style={{ backgroundColor: KIND_COLOR.get(kind.key) }}
          />
          <span className="text-muted-foreground">{kind.label}</span>
        </li>
      ))}
    </ul>
  );
}

/** One span: where it sat in the thread, and how long it held. */
function SpanRow({ bar }: { bar: Bar }) {
  // An unmeasured duration is drawn in the de-emphasis step rather than in the
  // series hue: the sliver is there so the span is not missing from the list, and
  // it must not read as a measurement.
  const measured = bar.durationMs != null;
  const title = [
    bar.name,
    KIND_LABEL.get(bar.kind),
    bar.startOffsetMs == null ? null : `+${formatSeconds(bar.startOffsetMs)} 시작`,
    measured ? formatSeconds(bar.durationMs) : "소요 시간 없음",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <li className={ROW}>
      <span
        className="truncate text-xxs"
        // Indented by containment depth, so a tool call under a model call reads as
        // being inside it rather than as the next thing that happened.
        style={{ paddingLeft: `${Math.min(bar.depth, 4) * 0.5}rem` }}
        title={bar.name}
      >
        {bar.name}
      </span>
      {/* Positioned against the whole thread, not scaled to its own length: the
          question is where the time went, so gaps and overlaps have to stay
          visible. */}
      {/* `overflow-hidden` as well as the clamp in `traceBars`: the fill's geometry
          is arithmetic on log data, and the last time it was wrong it painted a
          2,294,987px bar across a 526px track and out of the card. Belt and
          braces, because the failure is silent and disfiguring. */}
      <span
        className="relative block h-2 min-w-0 overflow-hidden rounded-sm bg-muted"
        title={title}
      >
        <span
          className="absolute inset-y-0 rounded-sm"
          style={{
            left: `${bar.offsetPercent}%`,
            width: `${bar.widthPercent}%`,
            backgroundColor: measured
              ? (KIND_COLOR.get(bar.kind) ?? SERIES_COLORS[0])
              : MUTED_MARK,
          }}
        />
      </span>
      <span className="text-right text-xxs tabular-nums text-muted-foreground">
        {measured ? formatSeconds(bar.durationMs) : "—"}
      </span>
    </li>
  );
}

/**
 * One agent's recent threads, each opening its span timeline.
 *
 * This is where the timeline lives now. It used to render under every assistant
 * message in the chat as "이 턴", which was wrong twice over: the panel repeated
 * itself once per turn, and the route it called returns spans for the *whole*
 * thread — the turn id in the URL was echoed back, never filtered on. So a
 * per-thread row is both tidier and the honest scope.
 *
 * Nothing is fetched until a row is opened. Logs Insights is a StartQuery → poll
 * round trip taking seconds, so a list of ten threads must not fire ten of them.
 */
function TraceRow({
  thread,
  subjects,
}: {
  thread: RecordThread;
  /** sub -> email from the same response; the chip shows the local part. */
  subjects?: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [traces, setTraces] = useState<ThreadTraces | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setTraces(await fetchThreadTraces(thread.thread_id));
    } catch (e) {
      // A failure is stated in place, in the same shape the server uses for its
      // own degraded answers, so the row has one rendering path rather than two.
      setTraces({
        thread_id: thread.thread_id,
        status: "unavailable",
        spans: [],
        sources: { traces: false },
        detail: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setLoading(false);
    }
  };

  const { totalMs, bars } = traces
    ? (traceBars(traces.spans) as { totalMs: number; bars: Bar[] })
    : { totalMs: 0, bars: [] };

  return (
    <li className="border-b border-border/50 last:border-0">
      <button
        type="button"
        className="flex w-full items-center gap-2 py-1.5 text-left text-xs hover:bg-muted/40"
        onClick={() => {
          setOpen(!open);
          // Fetched on first open only; reopening a row shows what it already has
          // and offers an explicit retry below.
          if (!open && !traces && !loading) void load();
        }}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3 shrink-0" />
        ) : (
          <ChevronRight className="size-3 shrink-0" />
        )}
        {/* The owner leads: when an admin reads everyone's threads, "who asked" is
            the first thing that tells two rows apart. Present only for an admin (a
            plain user's list is all their own). The server resolves the sub to an
            email when it can; the chip shows the local part and keeps the full
            sub in the tooltip, and an unresolved sub is shown shortened. */}
        {thread.owner_sub && (
          <span
            className="flex shrink-0 items-center gap-1 font-mono text-xxs text-muted-foreground"
            title={thread.owner_sub}
          >
            <User className="size-3" />
            {subjectLabel(thread.owner_sub, subjects)}
          </span>
        )}
        {/* The opening question — what a reader recognises a thread by. The full
            text is in the tooltip, and a titleless row says so plainly rather than
            borrowing the session id as a name. */}
        <span
          className="min-w-0 flex-1 truncate"
          title={thread.title || undefined}
        >
          {thread.title || (
            <span className="text-muted-foreground/60">제목 없음</span>
          )}
        </span>
        <span className="w-20 shrink-0 tabular-nums text-muted-foreground">
          {formatThreadTime(thread.updated_at)}
        </span>
        {/* "저장된 답변", not "턴". This counts assistant messages in the stored
            transcript, while the leaderboard's 턴 column counts the usage table's
            own counter — two sources, and they diverge for backfilled history and
            for turns that ended without a message. One word for both was inviting a
            reader to treat a difference as an error. */}
        <span className="shrink-0 tabular-nums text-muted-foreground">
          저장된 답변 {thread.turns.toLocaleString("ko-KR")}
        </span>
      </button>

      {open && (
        // An inset card, not more rows: the detail sits in a tinted panel with its
        // own padding so it reads as "inside this thread" rather than as three more
        // lines of the list. Everything below was one flat wall of xxs muted text —
        // the structure here is what tells the metadata apart from the timeline.
        <div className="px-2 pb-2">
          <div className="space-y-3 rounded-md bg-muted/40 p-2.5 text-xxs">
            {/* Metadata as an aligned label/value list: labels in a fixed column,
                dim and uppercase; values in stronger ink so the eye lands on the id
                and the counts, not the words naming them. The session id is here
                because it is the handle you reach for once looking at a thread —
                full and selectable, since a truncated id cannot be pasted. */}
            <dl className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-x-3 gap-y-1">
              <dt className="uppercase tracking-wider text-muted-foreground/60">
                세션
              </dt>
              <dd className="select-all break-all font-mono text-foreground/90">
                {thread.thread_id}
              </dd>
              {traces?.status === "ok" && (
                <>
                  <dt className="uppercase tracking-wider text-muted-foreground/60">
                    스팬
                  </dt>
                  <dd className="tabular-nums text-foreground/90">
                    {bars.length.toLocaleString("ko-KR")}개 · 전체{" "}
                    {formatSeconds(totalMs)}
                  </dd>
                </>
              )}
            </dl>

            {loading && (
              <span className="text-muted-foreground">
                스팬을 조회하고 있습니다…
              </span>
            )}

            {traces?.status === "timeout" && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <span>아직 조회가 끝나지 않았습니다.</span>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 text-info underline"
                  onClick={() => void load()}
                >
                  <RefreshCw className="size-3" /> 다시 시도
                </button>
              </div>
            )}
            {traces?.status === "empty" && (
              <span className="text-muted-foreground">
                이 스레드의 스팬이 없습니다. 추적을 켜기 전의 대화이거나, 계정에
                Transaction Search 가 꺼져 있을 수 있습니다.
              </span>
            )}
            {traces?.status === "unavailable" && (
              <span className="text-muted-foreground">{traces.detail}</span>
            )}

            {/* The timeline, fenced off from the metadata by a divider so the two
                do not read as one block. */}
            {traces?.status === "ok" && (
              <div className="space-y-1 border-t border-border/60 pt-2">
                <TimelineAxis totalMs={totalMs} />
                <ul className="space-y-1">
                  {bars.map((bar, index) => (
                    <SpanRow key={`${bar.name}-${index}`} bar={bar} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export function ThreadDrilldown({ recordId }: { recordId: string }) {
  const [threads, setThreads] = useState<RecordThread[] | null>(null);
  const [subjects, setSubjects] = useState<Record<string, string> | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setThreads(null);
    setError(null);
    // Ten, newest first. The list grows with every conversation an agent answers
    // and each open row fires a billed Logs Insights query, so an unbounded column
    // is both a wall of rows and a wall of cost waiting to be clicked.
    fetchRecordThreads(recordId, 10)
      .then((value) => {
        if (!live) return;
        setSubjects(value.subjects);
        setThreads(value.threads);
      })
      .catch((e) => {
        if (live) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, [recordId]);

  // The heading and its cost warning live on the `FoldBand` in the registry,
  // which is what lets the whole section fold to one line.
  return (
    <>
      {error ? (
        <EmptyPlot label={error} />
      ) : !threads ? (
        <EmptyPlot label="스레드를 읽고 있습니다…" />
      ) : threads.length === 0 ? (
        <EmptyPlot label="이 에이전트로 열린 스레드가 없습니다." />
      ) : (
        <div className="space-y-2">
          {/* The colour key once, above the rows it explains, rather than repeated
              inside each opened timeline. */}
          <SpanKindLegend />
          <ul className="rounded-md border border-border px-2">
            {threads.map((thread) => (
              <TraceRow key={thread.thread_id} thread={thread} subjects={subjects} />
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
