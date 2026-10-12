/**
 * Turning spans into a timeline the eye can read.
 *
 * The point of this panel is answering "where did the 90 seconds go", so the
 * bars must be positioned against the whole thread's duration — a bar chart of
 * durations alone would show which span was longest but not whether anything ran
 * in parallel or where the gaps were.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SPAN_KINDS,
  formatSeconds,
  spanKind,
  traceAxisTicks,
  traceBars,
} from "./turnTrace.mjs";

const spans = [
  { name: "model", start_time: "2026-08-16 10:00:00", duration_ms: 1000 },
  { name: "tool", start_time: "2026-08-16 10:00:02", duration_ms: 2000 },
];

/**
 * The shape the server actually sends.
 *
 * `trace_service` fills `start_time` from `startTimeUnixNano` and only falls back
 * to `@timestamp`, so epoch **nanoseconds as a digit string** is the normal case
 * and the `"2026-08-16 10:00:00"` form above is the exception. Every test in this
 * file used the exception, which is how a `Date.parse` that returns NaN on the
 * real payload shipped: `total` fell to its 1ms floor and every bar was drawn
 * ~430,000% wide from the left edge.
 */
const nanoSpans = [
  { name: "POST /invocations", start_time: "1787139505106178475", duration_ms: 11485.4 },
  { name: "chat", start_time: "1787139506390661811", duration_ms: 1399.5 },
  {
    name: "execute_tool platform-tools___web_search",
    start_time: "1787139507884735358",
    duration_ms: 4365.0,
  },
];

test("bars are positioned against the whole turn, not against each other", () => {
  const { bars } = traceBars(spans);

  assert.equal(bars[0].offsetPercent, 0);
  assert.equal(bars[0].widthPercent, 25);
  assert.equal(bars[1].offsetPercent, 50);
  assert.equal(bars[1].widthPercent, 50);
});

test("the total the bars were divided by comes back, so the axis can label it", () => {
  const { totalMs } = traceBars(spans);

  assert.equal(totalMs, 4000);
});

test("start offsets are reported in milliseconds from the first span", () => {
  const { bars } = traceBars(spans);

  assert.equal(bars[0].startOffsetMs, 0);
  assert.equal(bars[1].startOffsetMs, 2000);
});

test("a span with no duration still gets a visible sliver", () => {
  const { bars } = traceBars([
    { name: "unknown", start_time: "2026-08-16 10:00:00", duration_ms: null },
  ]);

  assert.ok(bars[0].widthPercent > 0, "a zero-width bar reads as absent");
});

test("an empty span list yields no bars rather than dividing by zero", () => {
  assert.deepEqual(traceBars([]), { totalMs: 0, bars: [] });
});

test("a single instantaneous span does not produce NaN", () => {
  const { bars } = traceBars([
    { name: "model", start_time: "2026-08-16 10:00:00", duration_ms: 0 },
  ]);

  assert.ok(Number.isFinite(bars[0].widthPercent));
  assert.ok(Number.isFinite(bars[0].offsetPercent));
});

test("spans with unparsable start times keep their order rather than vanishing", () => {
  const { bars } = traceBars([
    { name: "a", start_time: "not a date", duration_ms: 100 },
    { name: "b", start_time: "also not", duration_ms: 100 },
  ]);

  assert.deepEqual(bars.map((bar) => bar.name), ["a", "b"]);
});

test("depth counts ancestors inside the response, so the panel can indent", () => {
  const { bars } = traceBars([
    {
      name: "invoke",
      span_id: "a",
      parent_span_id: null,
      start_time: "2026-08-16 10:00:00",
      duration_ms: 3000,
    },
    {
      name: "model",
      span_id: "b",
      parent_span_id: "a",
      start_time: "2026-08-16 10:00:00",
      duration_ms: 1000,
    },
    {
      name: "tool",
      span_id: "c",
      parent_span_id: "b",
      start_time: "2026-08-16 10:00:01",
      duration_ms: 500,
    },
  ]);

  assert.deepEqual(bars.map((bar) => bar.depth), [0, 1, 2]);
});

test("a parent the query did not return is drawn at the root, not at a guess", () => {
  const { bars } = traceBars([
    {
      name: "orphan",
      span_id: "b",
      parent_span_id: "missing",
      start_time: "2026-08-16 10:00:00",
      duration_ms: 100,
    },
  ]);

  assert.equal(bars[0].depth, 0);
});

test("a parent cycle terminates instead of hanging the panel", () => {
  const { bars } = traceBars([
    {
      name: "a",
      span_id: "a",
      parent_span_id: "b",
      start_time: "2026-08-16 10:00:00",
      duration_ms: 100,
    },
    {
      name: "b",
      span_id: "b",
      parent_span_id: "a",
      start_time: "2026-08-16 10:00:00",
      duration_ms: 100,
    },
  ]);

  assert.ok(bars.every((bar) => Number.isFinite(bar.depth)));
});

test("axis ticks span the whole timeline, both ends included", () => {
  const ticks = traceAxisTicks(4000);

  assert.deepEqual(
    ticks.map((tick) => Math.round(tick.percent)),
    [0, 33, 67, 100],
  );
  assert.equal(ticks[0].label, "0.0초");
  assert.equal(ticks[3].label, "4.0초");
});

test("a timeline with no duration gets no ticks rather than a zero-width axis", () => {
  assert.deepEqual(traceAxisTicks(0), []);
});

test("seconds drop the decimal once the figure is long enough not to need it", () => {
  assert.equal(formatSeconds(1234), "1.2초");
  assert.equal(formatSeconds(42_000), "42초");
  assert.equal(formatSeconds(null), "—");
});

test("epoch nanosecond start times place bars, they do not collapse the total", () => {
  const { totalMs, bars } = traceBars(nanoSpans);

  // 1787139505106178475ns → 1787139516591ms is the last finish; the first start is
  // 1787139505106ms. The wall clock of the turn, not the 1ms floor.
  assert.ok(totalMs > 11_000 && totalMs < 12_000, `totalMs was ${totalMs}`);
  assert.equal(bars[0].offsetPercent, 0);
  assert.ok(bars[1].offsetPercent > 10, `chat was placed at ${bars[1].offsetPercent}%`);
  assert.ok(bars[2].offsetPercent > bars[1].offsetPercent);
});

test("a bar never runs past the track it is drawn in", () => {
  for (const set of [spans, nanoSpans]) {
    for (const bar of traceBars(set).bars) {
      assert.ok(
        bar.offsetPercent + bar.widthPercent <= 100 + 1e-9,
        `${bar.name} ends at ${bar.offsetPercent + bar.widthPercent}%`,
      );
    }
  }
});

test("an instantaneous span at the very end is pulled inside the track", () => {
  // Its sliver would otherwise start at 100% and hang MIN_WIDTH_PERCENT past the
  // right edge — the same overflow as the nanosecond bug, two orders smaller.
  const { bars } = traceBars([
    { name: "start", start_time: "2026-08-16 10:00:00", duration_ms: 1000 },
    { name: "tail", start_time: "2026-08-16 10:00:01", duration_ms: 0 },
  ]);

  assert.ok(bars[1].offsetPercent < 100);
  assert.ok(bars[1].offsetPercent + bars[1].widthPercent <= 100);
});

test("spans are labelled by what they are, so colour can carry a meaning", () => {
  assert.equal(spanKind("POST /invocations"), "request");
  assert.equal(spanKind("invoke_agent Strands Agents"), "agent");
  assert.equal(spanKind("execute_event_loop_cycle"), "agent");
  assert.equal(spanKind("chat"), "model");
  assert.equal(
    spanKind("chat global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "model",
  );
  assert.equal(spanKind("execute_tool platform-tools___web_search"), "tool");
  assert.equal(spanKind("mcp tools/call platform-tools___fetch_url"), "tool");
  assert.equal(spanKind("mcp tools/list"), "tool");
  assert.equal(spanKind("Bedrock AgentCore.CreateEvent"), "memory");
  assert.equal(spanKind("Bedrock AgentCore.ListEvents"), "memory");
  assert.equal(spanKind("Cognito Identity Provider.InitiateAuth"), "auth");
  assert.equal(spanKind("something nobody has seen"), "other");
});

test("every kind a span can be given has a legend entry to explain it", () => {
  const keys = new Set(SPAN_KINDS.map((kind) => kind.key));
  const names = [
    "POST /invocations",
    "invoke_agent Strands Agents",
    "chat",
    "execute_tool x",
    "Bedrock AgentCore.CreateEvent",
    "Cognito Identity Provider.InitiateAuth",
    "unrecognised",
  ];

  for (const name of names) assert.ok(keys.has(spanKind(name)), name);
  // Colour follows the kind by its index here, and there are eight series slots.
  assert.ok(SPAN_KINDS.length <= 8);
});

test("bars carry their kind, so the timeline and its legend cannot disagree", () => {
  const { bars } = traceBars(nanoSpans);

  assert.deepEqual(
    bars.map((bar) => bar.kind),
    ["request", "model", "tool"],
  );
});
