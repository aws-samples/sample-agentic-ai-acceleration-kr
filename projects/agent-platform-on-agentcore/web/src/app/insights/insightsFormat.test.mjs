/**
 * The rule this file exists to protect: "0" and "unknown" must not look alike.
 *
 * A backfilled day carries no token attribute at all, so an agent that was
 * measured and spent nothing and an agent that was never measured arrive as
 * different data and must leave as different text. Collapsing them makes an
 * unused agent indistinguishable from an unmeasured one, which is the exact
 * failure that makes a cost dashboard worthless.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cacheHitRate,
  costAnomalies,
  costComposition,
  dailyCostPoints,
  dailySeries,
  guardrailActionLabel,
  guardrailEventRows,
  diffLabel,
  errorTone,
  formatCost,
  formatDelta,
  formatErrorRate,
  formatLatency,
  billedGap,
  coverageHint,
  formatMicros,
  formatRate,
  formatTokens,
  idleShare,
  mergeByName,
  periodDelta,
  rateOf,
  shareOf,
  shortDate,
  shortSubject,
  subjectLabel,
  sortNotices,
  sortRows,
  topNWithOther,
  trendSpec,
  usd,
  rateSourceLabel,
  formatUsdPer1m,
  rateCandidateNote,
  missingTiers,
  overlaySummary,
} from "./insightsFormat.mjs";

test("a measured zero renders as zero", () => {
  assert.equal(formatTokens(0, 0), "0");
});

test("a zero beside unrecorded turns is a dash, not zero and not a word", () => {
  assert.equal(formatTokens(0, 12), "—");
});

test("a partly recorded figure is the recorded sum, undecorated", () => {
  assert.equal(formatTokens(25324, 4), "25,324");
});

test("a fully measured figure carries no plus sign", () => {
  assert.equal(formatTokens(25324, 0), "25,324");
});

test("an absent figure is a dash", () => {
  assert.equal(formatTokens(null, 0), "—");
  assert.equal(formatTokens(undefined, 0), "—");
});

test("coverageHint names every kind of turn outside the cost figure, or nothing", () => {
  assert.equal(coverageHint({ turns: 10, unmeasured_turns: 0, priced_turns: 10, unpriced_turns: 0 }), null);
  assert.equal(
    coverageHint({ turns: 100, unmeasured_turns: 29, priced_turns: 48, unpriced_turns: 0 }),
    "비용 미산정 23턴 · 토큰 기록 없음 29턴 제외",
  );
  assert.equal(
    coverageHint({ turns: 10, unmeasured_turns: 2, priced_turns: 4, unpriced_turns: 4 }),
    "요율 미등록 4턴 · 토큰 기록 없음 2턴 제외",
  );
  assert.equal(coverageHint(undefined), null);
});

test("billedGap says which way the bill differs, in words", () => {
  assert.equal(billedGap(null), "집계 대기");
  assert.equal(billedGap({ runtime_micros: null }), "집계 대기");
  assert.equal(billedGap({ runtime_micros: 12, runtime_pct: 0.0001 }), "일치");
  assert.equal(billedGap({ runtime_micros: -755896, runtime_pct: -0.077886 }), "청구서가 7.8% 적음 ($0.7559)");
  assert.equal(billedGap({ runtime_micros: 250000, runtime_pct: 0.031 }), "청구서가 3.1% 많음 ($0.2500)");
  assert.equal(billedGap({ runtime_micros: 250000, runtime_pct: null }), "청구서가 $0.2500 많음");
});

test("a null billed figure is a dash, never free", () => {
  assert.equal(formatCost(null), "—");
  assert.equal(formatCost(undefined), "—");
  assert.equal(formatCost(0.5), "$0.5000");
});
test("cost keeps four decimals so sub-cent estimates stay visible", () => {
  assert.equal(formatCost(0.08123456), "$0.0812");
});

test("missing CloudWatch figures render as a dash, not a zero", () => {
  assert.equal(formatLatency(null), "—");
  assert.equal(formatErrorRate(null), "—");
  assert.equal(formatLatency(1234), "1.2초");
  assert.equal(formatErrorRate(0.1428), "14.3%");
});

test("sorting is stable and puts unknowns last in both directions", () => {
  const rows = [
    { record_id: "a", latency_p90_ms: 900 },
    { record_id: "b", latency_p90_ms: null },
    { record_id: "c", latency_p90_ms: 1500 },
  ];

  assert.deepEqual(
    sortRows(rows, "latency_p90_ms", "desc").map((r) => r.record_id),
    ["c", "a", "b"],
  );
  assert.deepEqual(
    sortRows(rows, "latency_p90_ms", "asc").map((r) => r.record_id),
    ["a", "c", "b"],
  );
});

test("sorting does not mutate the array it was given", () => {
  const rows = [{ record_id: "a", turns: 1 }, { record_id: "b", turns: 9 }];
  sortRows(rows, "turns", "desc");
  assert.equal(rows[0].record_id, "a");
});

test("the trend spec breaks the line on an unmeasured day instead of drawing zero", () => {
  const spec = trendSpec(
    [
      { date: "2026-08-14", input_tokens: 0, output_tokens: 0, tokens_known: false },
      { date: "2026-08-15", input_tokens: 500, output_tokens: 50, tokens_known: true },
    ],
    "input_tokens",
    "일별 입력 토큰",
  );

  assert.equal(spec.kind, "line");
  assert.equal(spec.data[0].input_tokens, null, "an unmeasured day must be a gap");
  assert.equal(spec.data[1].input_tokens, 500);
  assert.equal(spec.encoding.x, "date");
});

test("a turns trend is never gapped — turns are always measured", () => {
  const spec = trendSpec(
    [{ date: "2026-08-14", turns: 4, tokens_known: false }],
    "turns",
    "일별 턴",
  );

  assert.equal(spec.data[0].turns, 4);
});

test("dailySeries gaps token fields on an unmeasured day and never gaps turns", () => {
  const series = dailySeries([
    {
      date: "2026-08-14",
      turns: 4,
      input_tokens: 0,
      output_tokens: 0,
      tokens_known: false,
    },
    {
      date: "2026-08-15",
      turns: 9,
      input_tokens: 500,
      output_tokens: 50,
      tokens_known: true,
    },
  ]);

  assert.equal(series[0].turns, 4, "turns are always measured");
  assert.equal(series[0].input_tokens, null, "an unmeasured day must be a gap");
  assert.equal(series[0].output_tokens, null);
  assert.equal(series[1].input_tokens, 500);
  assert.equal(
    series[0].label,
    "8/14",
    "the axis carries a short label, not an ISO date",
  );
  assert.equal(
    series[0].date,
    "2026-08-14",
    "the full date stays for the tooltip",
  );
});

test("dailySeries tolerates an empty window", () => {
  assert.deepEqual(dailySeries([]), []);
  assert.deepEqual(dailySeries(undefined), []);
});

test("shortDate reads the string rather than constructing a Date", () => {
  // Parsed positionally on purpose: `new Date("2026-08-14")` is UTC midnight,
  // which renders as the 13th for every reader west of Greenwich.
  assert.equal(shortDate("2026-08-14"), "8/14");
  assert.equal(shortDate("2026-01-05"), "1/5");
  assert.equal(shortDate(""), "");
  assert.equal(shortDate(null), "");
});

test("periodDelta compares the window's later half against its earlier half", () => {
  const daily = [
    { date: "d1", turns: 10, tokens_known: true },
    { date: "d2", turns: 10, tokens_known: true },
    { date: "d3", turns: 15, tokens_known: true },
    { date: "d4", turns: 15, tokens_known: true },
  ];
  const delta = periodDelta(daily, "turns");
  assert.equal(delta.earlier, 20);
  assert.equal(delta.later, 30);
  assert.equal(delta.pct, 50);
  assert.equal(delta.half, 2, "the label names the days actually compared");
});

test("periodDelta's half counts the rows it has, not the window that was asked for", () => {
  // A window with three rows compares one day against one day. Labelling that
  // "앞 15일 대비" would name a period that was never compared.
  //
  // The rows *are* the days: `daily_totals` sends one point per date in the window,
  // idle days included. It used to send only the days that had traffic, so this
  // split by row count silently compared two days that were nowhere near each
  // other — see the test below.
  const daily = [
    { date: "2026-08-14", turns: 5, tokens_known: true },
    { date: "2026-08-15", turns: 5, tokens_known: true },
    { date: "2026-08-16", turns: 10, tokens_known: true },
  ];
  assert.equal(periodDelta(daily, "turns").half, 1);
});

test("an idle day occupies the axis, so the halves are the calendar halves", () => {
  // Measured against the old sparse series: a 7-day window with rows on 11, 12, 16
  // and 17 dropped the partial day, split the remaining three by count, and
  // compared 08-11 against 08-16 — five days apart — under the label "앞 1일 대비".
  // With every day present, three days really are compared against three.
  const daily = [
    { date: "2026-08-11", turns: 10, tokens_known: true },
    { date: "2026-08-12", turns: 10, tokens_known: true },
    { date: "2026-08-13", turns: 0, tokens_known: true, filled: true },
    { date: "2026-08-14", turns: 0, tokens_known: true, filled: true },
    { date: "2026-08-15", turns: 0, tokens_known: true, filled: true },
    { date: "2026-08-16", turns: 20, tokens_known: true },
    { date: "2026-08-17", turns: 4, tokens_known: true },
  ];

  const delta = periodDelta(daily, "turns", "2026-08-17");

  assert.equal(delta.half, 3, "the label names three days because three were compared");
  assert.equal(delta.earlier, 20, "08-11 + 08-12 + 08-13");
  assert.equal(delta.later, 20, "08-14 + 08-15 + 08-16");
});

test("a filled day does not gap the token line, because zero turns spent zero tokens", () => {
  const daily = [
    { date: "2026-08-14", input_tokens: 100, turns: 2, tokens_known: true },
    { date: "2026-08-15", input_tokens: 0, turns: 0, tokens_known: true, filled: true },
    { date: "2026-08-16", input_tokens: 300, turns: 4, tokens_known: true },
    { date: "2026-08-17", input_tokens: 0, turns: 0, tokens_known: true },
  ];

  const series = dailySeries(daily, "2026-08-17");
  assert.equal(series[1].input_tokens, 0, "an idle day is a measured zero");
  assert.notEqual(periodDelta(daily, "input_tokens", "2026-08-17"), null);
});

test("periodDelta drops the middle point of an odd window so the halves are equal", () => {
  const daily = [
    { date: "d1", turns: 10, tokens_known: true },
    { date: "d2", turns: 999, tokens_known: true },
    { date: "d3", turns: 20, tokens_known: true },
  ];
  const delta = periodDelta(daily, "turns");
  assert.equal(delta.earlier, 10);
  assert.equal(delta.later, 20);
  assert.equal(delta.pct, 100);
});

test("periodDelta refuses a comparison it cannot make", () => {
  assert.equal(periodDelta([], "turns"), null, "no data");
  assert.equal(
    periodDelta([{ date: "d1", turns: 4, tokens_known: true }], "turns"),
    null,
    "one point has no earlier half",
  );
  assert.equal(
    periodDelta(
      [
        { date: "d1", turns: 0, tokens_known: true },
        { date: "d2", turns: 8, tokens_known: true },
      ],
      "turns",
    ),
    null,
    "a zero baseline would be an infinite increase",
  );
});

test("periodDelta refuses a token comparison whose window is partly unmeasured", () => {
  // The floor is not a quantity you can subtract. A backfilled day carries no
  // token attribute, so "+38%" here would be arithmetic on a number we know is
  // incomplete — the strongest possible version of a wrong answer.
  const daily = [
    { date: "d1", input_tokens: 100, tokens_known: false },
    { date: "d2", input_tokens: 400, tokens_known: true },
  ];
  assert.equal(periodDelta(daily, "input_tokens"), null);
  assert.equal(
    periodDelta(daily, "turns") === null,
    true,
    "turns still need a non-zero baseline",
  );
});

test("formatDelta signs the change and states the one it cannot make", () => {
  assert.equal(formatDelta(null), "대조 불가");
  assert.equal(formatDelta({ pct: 49.6 }), "+50%");
  assert.equal(formatDelta({ pct: -12.4 }), "-12%");
  // Rounded first, then signed: "+0%" claims a rise that rounding invented.
  assert.equal(formatDelta({ pct: 0.2 }), "0%");
  assert.equal(formatDelta({ pct: 0 }), "0%");
});

test("topNWithOther folds the tail into one labelled slot", () => {
  const entries = [
    { name: "a", value: 5 },
    { name: "b", value: 30 },
    { name: "c", value: 20 },
    { name: "d", value: 10 },
  ];
  const folded = topNWithOther(entries, 3);

  assert.deepEqual(
    folded.map((e) => e.name),
    ["b", "c", "기타"],
    "sorted by value, with the tail in the last slot",
  );
  assert.equal(folded[2].value, 15, "the tail keeps its total");
  assert.equal(folded[2].folded, 2, "and says how many rows it stands for");
});

test("topNWithOther leaves a short list alone and never invents a ninth colour", () => {
  const entries = [
    { name: "a", value: 1 },
    { name: "b", value: 2 },
  ];
  const kept = topNWithOther(entries, 8);
  assert.deepEqual(
    kept.map((e) => e.name),
    ["b", "a"],
  );
  assert.equal(kept[0].folded, undefined);
  assert.deepEqual(topNWithOther([], 8), []);
  assert.deepEqual(topNWithOther(undefined, 8), []);
});

test("sortNotices puts the worst first and keeps equals in the order they were raised", () => {
  const sorted = sortNotices([
    { id: "info-1", tone: "info" },
    { id: "warn-1", tone: "warning" },
    { id: "info-2", tone: "info" },
    { id: "error-1", tone: "error" },
  ]);

  assert.deepEqual(
    sorted.map((notice) => notice.id),
    ["error-1", "warn-1", "info-1", "info-2"],
  );
  // An unknown tone sorts last rather than ahead of a real error.
  assert.deepEqual(
    sortNotices([
      { id: "odd", tone: "chatty" },
      { id: "e", tone: "error" },
    ]).map((n) => n.id),
    ["e", "odd"],
  );
  assert.deepEqual(sortNotices([]), []);
  assert.deepEqual(sortNotices(undefined), []);
});

test("mergeByName folds a tail that collides with a real category", () => {
  // Cost Explorer's own "other" bucket renders as 기타, and topNWithOther names
  // its tail 기타 as well. Left alone that is a duplicate React key and a
  // segment drawn twice at two different widths.
  const merged = mergeByName([
    { name: "Runtime", value: 4 },
    { name: "기타", value: 1 },
    { name: "기타", value: 2, folded: 3 },
  ]);

  assert.equal(merged.length, 2);
  assert.deepEqual(
    merged.map((e) => e.name),
    ["Runtime", "기타"],
  );
  assert.equal(merged[1].value, 3);
  assert.equal(
    merged[1].folded,
    4,
    "the fold counts both the tail and the bucket",
  );
  assert.deepEqual(mergeByName([]), []);
  assert.deepEqual(mergeByName(undefined), []);
});

test("errorTone keeps an unmeasured rate distinct from a clean one", () => {
  assert.equal(errorTone(null), "unknown", "CloudWatch silence is not health");
  assert.equal(errorTone(undefined), "unknown");
  assert.equal(errorTone(0), "none");
  assert.equal(errorTone(0.019), "none");
  assert.equal(errorTone(0.02), "warn");
  assert.equal(errorTone(0.0999), "warn");
  assert.equal(errorTone(0.1), "bad");
  assert.equal(errorTone(1), "bad");
});

test("shareOf is a clamped fraction and never divides by nothing", () => {
  assert.equal(shareOf(25, 100), 0.25);
  assert.equal(shareOf(5, 0), 0, "an empty window is not an infinite share");
  assert.equal(shareOf(null, 100), 0);
  assert.equal(
    shareOf(150, 100),
    1,
    "clamped, so a bar never overflows its track",
  );
});

test("the partial day is excluded from the delta", () => {
  // The window ends *now*, so its last row holds however much of today has
  // happened — and it lands in the later half. Comparing a fraction of today
  // against whole days reported the missing remainder as a fall in usage: a
  // structural, always-negative bias, largest first thing in the morning.
  const daily = [
    { date: "2026-08-11", turns: 100, tokens_known: true },
    { date: "2026-08-12", turns: 100, tokens_known: true },
    { date: "2026-08-13", turns: 100, tokens_known: true },
    { date: "2026-08-14", turns: 100, tokens_known: true },
    // Today, 20% elapsed. A flat platform, not a collapsing one.
    { date: "2026-08-15", turns: 20, tokens_known: true },
  ];

  assert.equal(periodDelta(daily, "turns", "2026-08-15").pct, 0);
  // Left in, the same data reads as a 40% drop that never happened.
  assert.ok(periodDelta(daily, "turns").pct < -30);
});

test("dropping the partial day can leave nothing to compare", () => {
  // One whole day plus today is not two halves. Better to say so than to compare
  // a day against itself.
  const daily = [
    { date: "2026-08-14", turns: 10, tokens_known: true },
    { date: "2026-08-15", turns: 2, tokens_known: true },
  ];
  assert.equal(periodDelta(daily, "turns", "2026-08-15"), null);
});

test("the partial day is marked on the axis label", () => {
  const rows = dailySeries(
    [
      { date: "2026-08-14", turns: 10, tokens_known: true },
      { date: "2026-08-15", turns: 2, tokens_known: true },
    ],
    "2026-08-15",
  );

  assert.equal(rows[0].label, "8/14");
  assert.equal(rows[0].partial, false);
  // A short bar labelled "8/15" is a drop; "8/15*" is an hour of one.
  assert.equal(rows[1].label, "8/15*");
  assert.equal(rows[1].partial, true);
});

test("a rate with no denominator is unknown, not zero", () => {
  // "0% failed" invented out of an absence is a clean bill of health nobody earned.
  assert.equal(rateOf(0, 0), null);
  assert.equal(formatRate(rateOf(0, 0)), "—");
  assert.equal(formatRate(rateOf(3, 1000)), "0.3%");
});

test("a failure rate keeps one decimal", () => {
  // Rounded to whole percents, a platform dropping one turn in 250 reads as
  // flawless.
  assert.equal(formatRate(rateOf(4, 1000)), "0.4%");
});

test("the cache hit rate divides by every input-side token", () => {
  // Reads over reads + writes + uncached input: the question is how much of what
  // we sent avoided full price. Output is never cached and is not in it.
  const rate = cacheHitRate({
    input_tokens: 1000,
    output_tokens: 9999,
    cache_read_tokens: 8000,
    cache_write_tokens: 1000,
  });
  assert.equal(rate.rate, 0.8);
  assert.equal(formatRate(rate), "80.0%");
});

test("no input-side tokens means no cache rate rather than 0%", () => {
  assert.equal(cacheHitRate({ output_tokens: 500 }), null);
  assert.equal(cacheHitRate({}), null);
});

test("an error rate built on one of its two series is marked, not silently halved", () => {
  // `SystemErrors` and `UserErrors` are separate series and either can be missing.
  // The numerator skipped the absent one, so half the errors read as all of them.
  assert.equal(formatErrorRate(0.1, ["system_errors", "user_errors"]), "10.0%");
  assert.equal(formatErrorRate(0.1, ["system_errors"]), "10.0%*");
  // No basis reported at all (an older payload) claims nothing either way.
  assert.equal(formatErrorRate(0.1), "10.0%");
  assert.equal(formatErrorRate(null, ["system_errors"]), "—");
});

test("a subject is shortened without assuming it is a UUID", () => {
  // A federated identity can be an opaque string with no hyphen, and splitting on
  // one would render the whole thing.
  assert.equal(shortSubject("3f9a1c2d-1111-2222-3333-444455556666"), "3f9a1c2d…");
  assert.equal(shortSubject("abc"), "abc");
  assert.equal(shortSubject(""), "—");
  assert.equal(shortSubject(null), "—");
});

test("the daily rows carry tool calls and both endings", () => {
  // A comment on the KPI tiles claimed they did not — "the daily rows carry turns
  // and tokens only, and a trend drawn from anything else here would be
  // interpolation". `daily_totals` folds every counter, so they were there all
  // along and the tiles were showing dead space instead of a trend.
  const rows = dailySeries([
    {
      date: "2026-08-14",
      turns: 4,
      tool_calls: 43,
      failed_turns: 1,
      interrupted_turns: 2,
      tokens_known: false,
    },
  ]);

  assert.equal(rows[0].tool_calls, 43);
  assert.equal(rows[0].failed_turns, 1);
  assert.equal(rows[0].interrupted_turns, 2);
  // Counts, so never gapped — only the token fields become null on an unmeasured
  // day, because only they are the ones we would be asserting we had measured.
  assert.equal(rows[0].input_tokens, null);
  assert.equal(rows[0].turns, 4);
});

test("dailySeries carries guardrail interventions as a plain count, zero when absent", () => {
  const series = dailySeries([
    { date: "2026-08-21", turns: 3, tokens_known: true, guardrail_interventions: 2 },
    { date: "2026-08-22", turns: 1, tokens_known: true },
  ]);
  assert.equal(series[0].guardrail_interventions, 2);
  assert.equal(series[1].guardrail_interventions, 0);
});

// --- exact ledger: integer micro-dollars in, one honest string out ----------

test("formatMicros renders exact dollars and names the reason for a gap", () => {
  assert.equal(formatMicros(1234567), "$1.2346");
  assert.equal(formatMicros(0), "$0.0000");
  assert.equal(formatMicros(null, { unpriced: 2 }), "요율 미등록");
  assert.equal(formatMicros(null, { unmeasured: 3 }), "—");
  assert.equal(formatMicros(null, { unpriced: 2, unmeasured: 3 }), "요율 미등록");
  assert.equal(formatMicros(null), "—");
  assert.equal(formatMicros(undefined), "—");
});

test("usd converts micros to a number for charts", () => {
  assert.equal(usd(2_100_000), 2.1);
  assert.equal(usd(null), null);
});

test("diffLabel calls sub-0.1% a match and signs the rest", () => {
  assert.equal(diffLabel({ runtime_micros: 500, runtime_pct: 0.0005 }), "일치");
  assert.equal(diffLabel({ runtime_micros: 120000, runtime_pct: 0.034 }), "+$0.1200 (+3.4%)");
  assert.equal(diffLabel({ runtime_micros: -50000, runtime_pct: -0.012 }), "-$0.0500 (-1.2%)");
  assert.equal(diffLabel(null), "집계 대기");
});

test("costComposition splits the cost block into labelled shares", () => {
  const rows = costComposition({
    model: { micros: 2_000_000 },
    runtime: { micros: 1_000_000, active_micros: 100_000, idle_micros: 900_000 },
    gateway: { micros: 1_000 },
    memory: { micros: null, billed_micros: 5_000 },
    total_micros: 3_001_000,
  });
  assert.deepEqual(
    rows.map((r) => [r.key, r.micros, r.source]),
    [
      ["model", 2_000_000, "ledger"],
      ["runtime_active", 100_000, "metered"],
      ["runtime_idle", 900_000, "metered"],
      ["gateway", 1_000, "metered"],
      ["memory", 5_000, "billed"],
    ],
  );
  assert.equal(rows[0].label, "모델");
  assert.ok(Math.abs(rows[0].share - 2_000_000 / 3_006_000) < 1e-9);
});

test("costComposition leaves out tiers that have nothing", () => {
  const rows = costComposition({ model: { micros: 10 }, runtime: { micros: null }, gateway: { micros: null }, memory: { micros: null, billed_micros: null } });
  assert.deepEqual(rows.map((r) => r.key), ["model"]);
});

test("idleShare is memory over runtime, absent without a runtime figure", () => {
  assert.equal(idleShare({ micros: 1_000_000, active_micros: 250_000, idle_micros: 750_000 }), 0.75);
  assert.equal(idleShare({ micros: null }), null);
});

test("costAnomalies works on micro-dollar daily runtime points", () => {
  const daily = [
    { date: "2026-09-01", runtime_cost_micros: 100 },
    { date: "2026-09-02", runtime_cost_micros: 100 },
    { date: "2026-09-03", runtime_cost_micros: 100 },
    { date: "2026-09-04", runtime_cost_micros: 500 },
    { date: "2026-09-05", runtime_cost_micros: 100 },
  ];
  const out = costAnomalies(dailyCostPoints(daily), { partialDay: "2026-09-05" });
  assert.deepEqual(out.map((o) => o.date), ["2026-09-04"]);
});

test("formatMicros renders a partly-unmeasured cost plainly and an all-unmeasured zero as a dash", () => {
  assert.equal(formatMicros(1234567, { unmeasured: 3 }), "$1.2346");
  assert.equal(formatMicros(0, { unmeasured: 3 }), "—");
  assert.equal(formatMicros(0), "$0.0000");
});

test("guardrailActionLabel names the stage and the action in Korean", () => {
  assert.equal(guardrailActionLabel("BLOCKED", "input"), "입력 차단");
  assert.equal(guardrailActionLabel("ANONYMIZED", "output"), "출력 마스킹");
  assert.equal(guardrailActionLabel("WEIRD", "input"), "WEIRD");
});

test("a subject with an email shows the part before the @, otherwise the shortened sub", () => {
  // The pool signs in by email, so the sub is a UUID nobody recognises. An admin
  // reads "alice", not "alice@example.com" (the domain is the same for everyone
  // here) and not "3f9a1c2d…". A sub the directory could not name stays a sub.
  const subjects = { "3f9a1c2d-1111-2222-3333-444455556666": "alice@example.com" };
  assert.equal(subjectLabel("3f9a1c2d-1111-2222-3333-444455556666", subjects), "alice");
  assert.equal(subjectLabel("0123456789abcdef", subjects), "01234567…");
  assert.equal(subjectLabel("0123456789abcdef", undefined), "01234567…");
  assert.equal(subjectLabel("", subjects), "—");
  // An email without an @ (a directory that returned a username) is shown whole.
  assert.equal(subjectLabel("s", { s: "alice" }), "alice");
});

test("guardrailEventRows names a user from the subjects map when it can", () => {
  const rows = guardrailEventRows(
    [{ at: "2026-09-23T10:00:00Z", agent_record_id: "rec-1", owner_sub: "0123456789abcdef",
       thread_id: "t-1", turn_id: "t-1:m-1", action: "BLOCKED", stage: "input",
       filter_types: [], confidences: [] }],
    [],
    { "0123456789abcdef": "alice@example.com" },
  );
  assert.equal(rows[0].user, "alice");
});

test("guardrailEventRows resolves agent names, shortens subjects and keeps the thread id", () => {
  const rows = guardrailEventRows(
    [
      { at: "2026-09-23T10:00:00Z", agent_record_id: "rec-1", owner_sub: "0123456789abcdef",
        thread_id: "t-1", turn_id: "t-1:m-1", action: "BLOCKED", stage: "input",
        filter_types: ["INSULTS", "PROMPT_ATTACK"], confidences: ["HIGH"] },
      { at: "2026-09-23T09:00:00Z", agent_record_id: "rec-9", owner_sub: "",
        thread_id: "", turn_id: "", action: "ANONYMIZED", stage: "output",
        filter_types: [], confidences: [] },
    ],
    [{ record_id: "rec-1", name: "bap_default" }],
  );
  assert.equal(rows.length, 2);
  assert.equal(rows[0].agent, "bap_default");
  assert.equal(rows[0].user, "01234567…");
  assert.equal(rows[0].filters, "INSULTS, PROMPT_ATTACK");
  assert.equal(rows[0].action, "입력 차단");
  assert.equal(rows[0].threadId, "t-1");
  // Unknown agent falls back to the id; no owner and no thread are shown as such.
  assert.equal(rows[1].agent, "rec-9");
  assert.equal(rows[1].user, "—");
  assert.equal(rows[1].filters, "—");
  assert.equal(rows[1].threadId, null);
  // Keys are unique even when two events share a turn.
  assert.notEqual(rows[0].key, rows[1].key);
});

test("rate sources read as the admin's words, not the machine tag", () => {
  assert.equal(rateSourceLabel("card:2026-09-23.1"), "요율표");
  assert.equal(rateSourceLabel("bill"), "AWS 청구서");
  assert.equal(rateSourceLabel("Cost Explorer daily lines 2026-09-20..2026-09-21"), "AWS 청구서");
  assert.equal(rateSourceLabel("price-list"), "Price List");
  assert.equal(rateSourceLabel("admin:admin@example.com"), "직접 입력 · admin@example.com");
  assert.equal(rateSourceLabel(null), "—");
});

test("per-million rates show at least two decimals and never a stray exponent", () => {
  assert.equal(formatUsdPer1m("4"), "$4.00");
  assert.equal(formatUsdPer1m("1.375"), "$1.375");
  assert.equal(formatUsdPer1m("0.2"), "$0.20");
  assert.equal(formatUsdPer1m("20"), "$20.00");
  assert.equal(formatUsdPer1m(null), "—");
  assert.equal(formatUsdPer1m("abc"), "—");
});

test("a candidate's note states its evidence", () => {
  assert.equal(
    rateCandidateNote({ source: "bill", effective_from: "2026-09-23", last_day: "2026-09-24", days_observed: 2 }),
    "청구서 · 09-23~09-24 · 2일 관측",
  );
  assert.equal(
    rateCandidateNote({ source: "bill", effective_from: "2026-09-24", last_day: "2026-09-24", days_observed: 1 }),
    "청구서 · 09-24 · 1일 관측",
  );
  assert.equal(rateCandidateNote({ source: "price-list" }), "Price List 게시값");
});

test("a row opens with exactly the tiers it lacks", () => {
  const row = { tiers: { input: { usd_per_1m: "4" }, output: null, cache_read: null, cache_write: { usd_per_1m: "5" } } };
  assert.deepEqual(missingTiers(row), ["output", "cache_read"]);
  assert.deepEqual(missingTiers({ tiers: {} }), ["input", "output", "cache_read", "cache_write"]);
});

test("a family with a long card also lacks the long tiers the server sent empty", () => {
  const plain = { input: { usd_per_1m: "0.1" }, output: { usd_per_1m: "0.5" }, cache_read: { usd_per_1m: "0.01" }, cache_write: { usd_per_1m: "0.125" } };
  const row = { tiers: { ...plain, long_input: null, long_output: { usd_per_1m: "2.5" }, long_cache_read: null, long_cache_write: null } };
  assert.deepEqual(missingTiers(row), ["long_input", "long_cache_read", "long_cache_write"]);
  // A one-card family has no long tiers to be missing.
  assert.deepEqual(missingTiers({ tiers: plain }), []);
});

test("the overlay summary names each source instead of calling everything the bill's", () => {
  assert.equal(overlaySummary([]), null);
  assert.equal(
    overlaySummary([
      { family: "claude-opus-5-5", source: "admin:admin@example.com" },
      { family: "claude-opus-5-5", source: "admin:admin@example.com" },
      { family: "claude-sonnet-5", source: "Cost Explorer daily lines 2026-09-20..2026-09-21" },
      { family: "claude-haiku-4-5", source: "price-list" },
      { family: "claude-haiku-4-5", source: "bill" },
    ]),
    "요율표에 추가된 요율 5개 (청구서 학습 2 · Price List 1 · 직접 입력 2) · claude-haiku-4-5, claude-opus-5-5, claude-sonnet-5",
  );
});
