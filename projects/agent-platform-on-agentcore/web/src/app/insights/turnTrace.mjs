/**
 * Spans to positioned bars.
 *
 * Positions are relative to the whole thread's span, not to each other, because
 * the question this panel answers is "where did the time go" — and that needs
 * gaps and overlaps to be visible. A plain bar chart of durations shows which
 * span was longest and hides both.
 *
 * `totalMs` comes back with the bars rather than being recomputed by the view: the
 * denominator every percentage was divided by is the one thing an axis has to
 * label, and a panel that draws bars against one total and ticks against another
 * is worse than a panel with no ticks at all.
 */

const MIN_WIDTH_PERCENT = 0.5;

/** Guards a `parent_span_id` cycle; deeper than this is a broken trace, not a tree. */
const MAX_DEPTH = 8;

/**
 * What a span *is*, in the order colour is assigned to it.
 *
 * A timeline of sixteen identically coloured bars says only "something took a
 * while". These seven kinds are the vocabulary of one turn on this platform, so
 * hue can carry the answer to "what was it doing" and the reader gets it from the
 * legend rather than by reading sixteen span names.
 *
 * Colour follows the kind by its index in this array and is never re-packed, so a
 * thread that happens to make no tool calls does not repaint its model calls in
 * the tool colour. Keep the list at eight or fewer — that is how many categorical
 * series slots the design tokens define.
 */
export const SPAN_KINDS = [
  { key: "request", label: "요청" },
  { key: "agent", label: "에이전트 루프" },
  { key: "model", label: "모델 호출" },
  { key: "tool", label: "툴 호출" },
  { key: "memory", label: "메모리" },
  { key: "auth", label: "인증" },
  { key: "other", label: "기타" },
];

/**
 * Matched in order, first hit wins.
 *
 * Every pattern here was written against span names measured in this account, not
 * guessed from the OpenTelemetry conventions: `chat <model-id>` and `chat` are the
 * same call at two layers, `execute_tool x` and `mcp tools/call x` likewise, and
 * `Bedrock AgentCore.CreateEvent` is a memory write rather than an agent call.
 */
const KIND_PATTERNS = [
  ["tool", /^(execute_tool|mcp tools\/)/],
  ["model", /^(chat|converse|invoke_model)\b/i, /^Bedrock Runtime\./],
  ["agent", /^(invoke_agent|execute_event_loop_cycle)/],
  // Scoped to the event and memory operations: `Bedrock AgentCore.` also prefixes
  // runtime and gateway calls, which are not memory.
  ["memory", /^Bedrock AgentCore\..*(Event|Memory|Session)/],
  ["auth", /^(Cognito|STS\.|AssumeRole)/],
  ["request", /^(GET|POST|PUT|PATCH|DELETE|HEAD)\s/],
];

/** One span name to one of `SPAN_KINDS`; `other` when nothing matches. */
export function spanKind(name) {
  const text = typeof name === "string" ? name : "";
  for (const [key, ...patterns] of KIND_PATTERNS) {
    if (patterns.some((pattern) => pattern.test(text))) return key;
  }
  return "other";
}

/**
 * A span's start, in milliseconds since the epoch, or null if it cannot be placed.
 *
 * **Two shapes arrive on this field and only one of them is a date.**
 * `trace_service` fills `start_time` from `startTimeUnixNano` and falls back to
 * `@timestamp`, so the normal value is epoch *nanoseconds* as a digit string
 * (`"1787139505106178475"`) and only the fallback looks like `"2026-08-16
 * 10:00:00"`. `Date.parse` returns NaN on the nanosecond form, which made every
 * start unplaceable: `total` fell to its 1ms floor, so `duration / total` turned
 * each bar into hundreds of thousands of percent and every one of them was painted
 * from the left edge far past the card. Measured 2026-08-19 on a real thread:
 * a 526px track holding a 2,294,987px fill, `width: 436309%`.
 */
function startMs(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (text === "") return null;
  if (/^\d+$/.test(text)) {
    // Not truncated to whole milliseconds: sibling spans in one event-loop cycle
    // start a few hundred microseconds apart, and rounding would stack them.
    const ms = Number(text) / 1e6;
    return Number.isFinite(ms) ? ms : null;
  }
  const parsed = Date.parse(text.replace(" ", "T"));
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * How many ancestors a span has *inside this response*.
 *
 * A parent the query did not return counts as no parent: the panel indents to show
 * containment it can prove, and a span whose parent was dropped is drawn at the
 * root rather than at a guessed depth.
 */
function depthOf(span, byId) {
  let depth = 0;
  let parentId = span.parent_span_id;
  while (parentId && byId.has(parentId) && depth < MAX_DEPTH) {
    depth += 1;
    parentId = byId.get(parentId).parent_span_id;
  }
  return depth;
}

export function traceBars(spans) {
  if (!spans || spans.length === 0) return { totalMs: 0, bars: [] };

  const byId = new Map();
  for (const span of spans) {
    if (span.span_id) byId.set(span.span_id, span);
  }

  const parsed = spans.map((span) => ({
    name: span.name,
    start: startMs(span.start_time),
    duration: Number.isFinite(span.duration_ms) ? span.duration_ms : null,
    depth: depthOf(span, byId),
    kind: spanKind(span.name),
  }));

  const starts = parsed.map((span) => span.start).filter((v) => v !== null);
  const origin = starts.length ? Math.min(...starts) : 0;
  const finish = parsed.reduce((latest, span) => {
    if (span.start === null) return latest;
    return Math.max(latest, span.start + (span.duration ?? 0));
  }, origin);
  // A turn whose spans all landed in the same millisecond still needs a
  // denominator; 1ms keeps every ratio finite.
  const total = Math.max(finish - origin, 1);

  return {
    totalMs: total,
    bars: parsed.map((span) => {
      // Clamped rather than trusted. The percentages are arithmetic on data from a
      // Logs Insights row, and the panel draws them straight into `left`/`width` on
      // an absolutely positioned span — so a shape this function did not expect
      // paints outside the card instead of looking wrong inside it. The floor also
      // means an instantaneous span at the very end would otherwise start at 100%
      // and hang its sliver over the duration column.
      const widthPercent = Math.min(
        Math.max(((span.duration ?? 0) / total) * 100, MIN_WIDTH_PERCENT),
        100,
      );
      const rawOffset =
        span.start === null ? 0 : ((span.start - origin) / total) * 100;
      return {
        name: span.name,
        depth: span.depth,
        kind: span.kind,
        durationMs: span.duration,
        /** Milliseconds after the first span started. Null when we can't place it. */
        startOffsetMs: span.start === null ? null : span.start - origin,
        offsetPercent: Math.min(Math.max(rawOffset, 0), 100 - widthPercent),
        widthPercent,
      };
    }),
  };
}

/**
 * Ticks for the timeline's own axis.
 *
 * Four of them including both ends, labelled in seconds — enough to read a bar's
 * position off the axis, few enough that the labels never collide inside a card.
 * Positions are percentages of the same `totalMs` the bars were divided by.
 */
export function traceAxisTicks(totalMs, count = 4) {
  if (!Number.isFinite(totalMs) || totalMs <= 0) return [];
  const steps = Math.max(count - 1, 1);
  return Array.from({ length: steps + 1 }, (_, index) => {
    const ms = (totalMs * index) / steps;
    return { percent: (index / steps) * 100, label: formatSeconds(ms) };
  });
}

/**
 * A duration in seconds, at one decimal below ten seconds.
 *
 * Milliseconds are not offered: these spans are model calls and tool round trips,
 * and a tick reading "1243ms" costs four characters to say what "1.2초" says.
 */
export function formatSeconds(ms) {
  if (!Number.isFinite(ms)) return "—";
  const seconds = ms / 1000;
  if (seconds >= 10) return `${Math.round(seconds)}초`;
  return `${seconds.toFixed(1)}초`;
}
