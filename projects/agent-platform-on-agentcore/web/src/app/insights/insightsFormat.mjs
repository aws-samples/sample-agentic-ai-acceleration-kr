/**
 * Presentation rules for the insights page, kept pure so the one that matters
 * can be tested without a browser.
 *
 * That rule: **"0" and "unknown" must render differently.** Stage 1's backfill
 * writes turns with no token attribute at all, because usage was never stored
 * and there is nothing to recover — so an agent measured at zero and an agent
 * never measured arrive as genuinely different data. Collapsing them would make
 * every pre-adoption agent look free, which is the strongest possible version
 * of the wrong answer.
 */

const NUMBER = new Intl.NumberFormat("ko-KR");

/**
 * Tokens as recorded. A dash, never "0", when nothing was recorded — an absent
 * figure and a zero with unrecorded turns beside it are both "no number here".
 *
 * The count is the exact sum over the turns that reported tokens; the caller
 * states how many turns did not (see `coverageHint`) instead of decorating the
 * number. A "+" suffix and a "모름" cell were tried and read as doubt about the
 * figure itself, which is the opposite of what they meant.
 */
export function formatTokens(value, unmeasuredTurns = 0) {
  if (value === null || value === undefined) return "—";
  if (value === 0 && unmeasuredTurns > 0) return "—";
  return NUMBER.format(value);
}

/**
 * A dollar figure that is already a float (Cost Explorer's own numbers).
 *
 * Everything this platform computes travels as integer micros — use
 * `formatMicros` for those. `null` is stated, never rendered as free.
 */
export function formatCost(value) {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(4)}`;
}

/** CloudWatch figures are dashes when absent — absence is not zero latency. */
export function formatLatency(ms) {
  if (ms === null || ms === undefined) return "—";
  return `${(ms / 1000).toFixed(1)}초`;
}

/**
 * CloudWatch figures are dashes when absent — absence is not zero errors.
 *
 * `basis` is which of `SystemErrors`/`UserErrors` the numerator holds. Either series
 * can be missing for an agent, and the numerator simply skipped the missing one — so
 * a rate built on half the errors was published as *the* error rate. Marked with `*`
 * rather than withheld: an agent that has never had a user error has no such series
 * at all, so refusing would blank the rate on the healthiest agents.
 *
 * @param {number | null | undefined} rate
 * @param {string[] | null} [basis]
 */
export function formatErrorRate(rate, basis = null) {
  if (rate === null || rate === undefined) return "—";
  const partial = Array.isArray(basis) && basis.length > 0 && basis.length < 2;
  return `${(rate * 100).toFixed(1)}%${partial ? "*" : ""}`;
}

/**
 * Sort a copy, with unknowns pinned last regardless of direction.
 *
 * Unknowns sort last both ways on purpose: an agent CloudWatch has no data for
 * should not win "lowest latency", which is what treating null as 0 would do.
 */
export function sortRows(rows, key, direction = "desc") {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = left[key];
    const b = right[key];
    const aMissing = a === null || a === undefined;
    const bMissing = b === null || b === undefined;
    if (aMissing && bMissing) return 0;
    if (aMissing) return 1;
    if (bMissing) return -1;
    if (typeof a === "string" || typeof b === "string") {
      return sign * String(a).localeCompare(String(b), "ko");
    }
    return sign * (a - b);
  });
}

/**
 * A `ChartSpec` for the existing ChartRenderer — no new charting dependency.
 *
 * Token fields become `null` on an unmeasured day so recharts leaves a gap.
 * Drawing zero there would assert we measured nothing spent, which is the one
 * thing we know we cannot say. Turn counts are always measured, so they pass
 * through untouched.
 */
export function trendSpec(daily, field, title) {
  const gapped = field === "input_tokens" || field === "output_tokens";
  return {
    kind: "line",
    title,
    data: daily.map((point) => ({
      date: point.date,
      [field]: gapped && !point.tokens_known ? null : (point[field] ?? 0),
    })),
    encoding: { x: "date", y: field, color: null },
  };
}

/**
 * A day's axis label, read out of the ISO string.
 *
 * Positional parsing rather than `new Date(iso)`: that constructor reads a
 * date-only string as UTC midnight, so every reader west of Greenwich sees the
 * previous day on the axis. The server already sends the dates in the window's
 * own calendar; there is nothing to convert.
 */
export function shortDate(iso) {
  if (typeof iso !== "string") return "";
  const parts = iso.split("-");
  if (parts.length < 3) return "";
  return `${Number(parts[1])}/${Number(parts[2])}`;
}

/**
 * The daily window as chart rows: one row per day, both token fields gapped.
 *
 * Same rule as `trendSpec`, applied once for every chart on the page — an
 * unmeasured day becomes `null` so recharts leaves a hole, because drawing zero
 * would assert we measured nothing spent. Turns are always counted, so they
 * pass through. `label` is for the axis, `date` stays for the tooltip.
 *
 * @param {Array<Record<string, any>>} daily
 * @param {string | null} [partialDay]
 */
export function dailySeries(daily, partialDay = null) {
  return (daily ?? []).map((point) => ({
    date: point.date,
    // The axis label carries the marker, because the axis is the only place a
    // reader looks before reading the last column as a finished day. A short bar
    // labelled "8/17" is a drop; the same bar labelled "8/17*" is an hour of it.
    label: shortDate(point.date) + (partialDay && point.date === partialDay ? "*" : ""),
    partial: Boolean(partialDay && point.date === partialDay),
    turns: point.turns ?? 0,
    // All three were assumed absent from the daily rows — a comment on the KPI tiles
    // said "the daily rows carry turns and tokens only, and a trend drawn from
    // anything else here would be interpolation". They were there all along:
    // `daily_totals` folds every counter in `COUNTER_NAMES`, so these are counted per
    // day exactly like turns are, and no interpolation is involved.
    tool_calls: point.tool_calls ?? 0,
    failed_turns: point.failed_turns ?? 0,
    interrupted_turns: point.interrupted_turns ?? 0,
    input_tokens: point.tokens_known ? point.input_tokens ?? 0 : null,
    output_tokens: point.tokens_known ? point.output_tokens ?? 0 : null,
    cache_read_tokens: point.tokens_known ? point.cache_read_tokens ?? 0 : null,
    cache_write_tokens: point.tokens_known ? point.cache_write_tokens ?? 0 : null,
    // A count like turns, never gapped: a day the guardrail acted zero times is a
    // real zero, and a day it was absent is shown by `scanned_turns`, not here.
    guardrail_interventions: point.guardrail_interventions ?? 0,
  }));
}

/**
 * The window's later half against its earlier half, or `null`.
 *
 * There is no previous-window request to compare against, so the comparison is
 * made inside the window we already have. Five cases return `null` rather than
 * a number, and each one is a comparison that would be a lie:
 *
 * - fewer than two days *after the partial one is dropped*: no earlier half;
 * - a zero earlier half: every increase from nothing is infinite;
 * - a token field over a partly unmeasured window: the total is a floor, and a
 *   floor is not a quantity you can subtract;
 * - a non-finite result.
 *
 * **`partialDay` is dropped before anything is summed, and that is the point.**
 * The window ends *now*, so its last day holds however much of today has
 * happened — and the later half is where it landed. Comparing a third of today
 * against three whole days reported the missing two thirds as a fall in usage: on
 * a 7-day window with today 30% elapsed, a flat platform read about -8%. The bias
 * was structural, always negative, and largest first thing in the morning.
 *
 * An odd window drops its middle day so the halves weigh the same.
 *
 * The `@param` annotations are load-bearing: this module is plain JS consumed from
 * TypeScript, and without them `tsc` infers the parameter type from the default —
 * so `partialDay = null` becomes type `null` and every real call site is an error.
 *
 * @param {Array<Record<string, any>>} daily
 * @param {string} field
 * @param {string | null} [partialDay]
 */
export function periodDelta(daily, field, partialDay = null) {
  const points = (daily ?? []).filter(
    (point) => !partialDay || point.date !== partialDay,
  );
  const gapped = field === "input_tokens" || field === "output_tokens";
  if (gapped && points.some((point) => !point.tokens_known)) return null;

  const half = Math.floor(points.length / 2);
  if (half < 1) return null;

  const sum = (slice) =>
    slice.reduce((total, point) => total + (point[field] ?? 0), 0);
  const earlier = sum(points.slice(0, half));
  const later = sum(points.slice(points.length - half));
  if (earlier <= 0) return null;

  const pct = ((later - earlier) / earlier) * 100;
  if (!Number.isFinite(pct)) return null;
  // `half` is how many days each side actually holds, which is not days/2: a
  // 7-day window can carry three rows, and a label derived from the requested
  // window would then name a period that was never compared.
  return { earlier, later, pct, half };
}

/**
 * A rate out of a total: `{ rate, of }`, or `null` when there is no denominator.
 *
 * `null` rather than 0 for an empty denominator, so "no turns yet" cannot render as
 * "0% failed" — a clean bill of health invented out of an absence, which is the
 * same mistake `errorTone` refuses to make for CloudWatch.
 */
export function rateOf(part, whole) {
  const numerator = Number(part ?? 0);
  const denominator = Number(whole ?? 0);
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator)) return null;
  if (denominator <= 0) return null;
  return { rate: numerator / denominator, of: denominator, count: numerator };
}

/**
 * A percentage with one decimal, or a dash.
 *
 * One decimal rather than none: a failure rate is interesting at 0.4% and rounding
 * to "0%" would report a platform that drops one turn in 250 as flawless.
 */
export function formatRate(value) {
  if (value === null || value === undefined) return "—";
  const pct = (value.rate ?? value) * 100;
  if (!Number.isFinite(pct)) return "—";
  return `${pct.toFixed(1)}%`;
}

/**
 * The share of prompt tokens that were served from cache.
 *
 * Cache reads over all input-side tokens — reads plus writes plus uncached input —
 * because that is the denominator the ratio is interesting against: it answers
 * "how much of what we sent did we avoid paying full price for". Output is not in
 * it; output is never cached.
 *
 * `null` when no input-side tokens were recorded at all, which is a window with no
 * measured turns rather than a window with no cache hits.
 */
export function cacheHitRate(totals) {
  const read = Number(totals?.cache_read_tokens ?? 0);
  const write = Number(totals?.cache_write_tokens ?? 0);
  const fresh = Number(totals?.input_tokens ?? 0);
  const prompt = read + write + fresh;
  if (prompt <= 0) return null;
  return { rate: read / prompt, of: prompt, count: read };
}

/**
 * A delta, signed.
 *
 * Rounded before it is signed: at 0.2% "+0%" claims a rise that rounding
 * invented. Deliberately carries no tone — more usage is neither good nor bad,
 * and the status colours mean a verdict.
 */
export function formatDelta(delta) {
  if (!delta || delta.pct === null || delta.pct === undefined)
    return "대조 불가";
  const rounded = Math.round(delta.pct);
  if (rounded === 0) return "0%";
  return `${rounded > 0 ? "+" : ""}${rounded}%`;
}

/**
 * The top `limit` entries by value, with the tail folded into one slot.
 *
 * The tail is folded, never given a colour of its own: past the eight validated
 * series steps a ninth hue is indistinguishable from an existing one under CVD.
 * `folded` says how many rows the slot stands for, so the fold is visible
 * instead of looking like the list simply ended.
 */
export function topNWithOther(entries, limit) {
  const sorted = [...(entries ?? [])].sort(
    (left, right) => (right.value ?? 0) - (left.value ?? 0),
  );
  if (sorted.length <= limit) return sorted;

  const head = sorted.slice(0, limit - 1);
  const tail = sorted.slice(limit - 1);
  return [
    ...head,
    {
      name: "기타",
      value: tail.reduce((total, entry) => total + (entry.value ?? 0), 0),
      folded: tail.length,
    },
  ];
}

/**
 * Notices, most severe first, stable within a tone.
 *
 * The dashboard can raise five at once — no usage table, no CloudWatch, no Cost
 * Explorer, a backfilled window, an unsaved layout — and stacked full-width they
 * push every chart below the fold, which is the problem the charts were added to
 * fix. The rail shows the worst one and folds the rest, so this order decides
 * which one a reader sees without expanding.
 */
const NOTICE_RANK = { error: 0, warning: 1, info: 2 };

export function sortNotices(notices) {
  return [...(notices ?? [])]
    .map((notice, index) => ({ notice, index }))
    .sort(
      (left, right) =>
        (NOTICE_RANK[left.notice.tone] ?? 9) -
          (NOTICE_RANK[right.notice.tone] ?? 9) || left.index - right.index,
    )
    .map((entry) => entry.notice);
}

/**
 * Sum entries that share a name, keeping the first appearance's order.
 *
 * Needed because a fold can collide with a real category: Cost Explorer already
 * has an "other" bucket that renders as 기타, and `topNWithOther` names its own
 * tail 기타 too. Two rows with the same name are a duplicate React key and a
 * segment drawn twice, so they are merged into one.
 */
export function mergeByName(entries) {
  const merged = new Map();
  for (const entry of entries ?? []) {
    const existing = merged.get(entry.name);
    if (!existing) {
      merged.set(entry.name, { ...entry });
      continue;
    }
    existing.value = (existing.value ?? 0) + (entry.value ?? 0);
    const folded = (existing.folded ?? 1) + (entry.folded ?? 1);
    existing.folded = folded;
  }
  return [...merged.values()];
}

/**
 * How loudly an error rate should read: `unknown`, `none`, `warn` or `bad`.
 *
 * `unknown` is its own case and never becomes `none` — CloudWatch not answering
 * is not a clean bill of health. And there is no `good`: a zero error rate gets
 * plain ink rather than green, because "no errors recorded" over a window this
 * short is an absence, not an achievement worth a verdict colour.
 */
export function errorTone(rate) {
  if (rate === null || rate === undefined) return "unknown";
  if (rate >= 0.1) return "bad";
  if (rate >= 0.02) return "warn";
  return "none";
}

/**
 * The head of a Cognito subject — enough to tell two people apart in a list.
 *
 * A sub is a UUID and there is no name to show: resolving one would mean a
 * `ListUsers` call per read against a pool the insights service otherwise never
 * touches, so the id travels whole from the server and is shortened here. An
 * operator who needs the person looks the prefix up once in Cognito.
 *
 * Eight characters rather than the first hyphen-delimited group, because a sub is
 * not required to be a UUID — a federated identity can be an opaque string with no
 * hyphen in it, and splitting on one would then render the whole thing.
 */
export function shortSubject(sub) {
  if (typeof sub !== "string" || sub === "") return "—";
  return sub.length <= 8 ? sub : `${sub.slice(0, 8)}…`;
}

/**
 * The name an admin reads for a sub: the local part of the email the server's
 * directory resolved (`subjects`, sub -> email), else the shortened sub.
 *
 * The pool signs people in by email, so the sub is a UUID nobody recognises and
 * the token carries no name — the server looks it up in Cognito at read time and
 * sends the whole email. The domain is dropped here because it is the same for
 * everyone on one pool and only pushes the part that differs off the bar label.
 * An entry the directory could not resolve (deleted account, permission gap) is
 * absent, and the sub is shown as before rather than a blank.
 */
export function subjectLabel(sub, subjects) {
  const email = subjects && typeof sub === "string" ? subjects[sub] : undefined;
  if (typeof email === "string" && email !== "") {
    const at = email.indexOf("@");
    return at > 0 ? email.slice(0, at) : email;
  }
  return shortSubject(sub);
}

const GUARDRAIL_ACTION_LABEL = {
  "BLOCKED:input": "입력 차단",
  "BLOCKED:output": "출력 차단",
  "ANONYMIZED:input": "입력 마스킹",
  "ANONYMIZED:output": "출력 마스킹",
};

/** "BLOCKED"+"input" → "입력 차단"; an action this table does not know is shown raw. */
export function guardrailActionLabel(action, stage) {
  return GUARDRAIL_ACTION_LABEL[`${action}:${stage}`] ?? String(action ?? "");
}

/**
 * Intervention events as table rows: agent name for id, shortened subject,
 * filters joined, action in words, and the thread id when there is one.
 *
 * The event is a pointer, not a transcript — the filter type says *what kind*
 * of thing tripped the guardrail, and the thread id is how the admin reads the
 * actual message. Two events can share a turn (input and output), so the key
 * is the event's time plus its position, never the turn id alone.
 *
 * @param {Array<Record<string, any>>} events newest first, as the server sends them
 * @param {Array<{record_id: string, name: string}>} agents
 */
export function guardrailEventRows(events, agents, subjects) {
  const nameOf = new Map((agents ?? []).map((agent) => [agent.record_id, agent.name]));
  return (events ?? []).map((event, index) => ({
    key: `${event.at ?? ""}#${index}`,
    at: event.at ?? "",
    agent: nameOf.get(event.agent_record_id) ?? event.agent_record_id ?? "—",
    user: event.owner_sub ? subjectLabel(event.owner_sub, subjects) : "—",
    filters: Array.isArray(event.filter_types) && event.filter_types.length > 0
      ? event.filter_types.join(", ")
      : "—",
    action: guardrailActionLabel(event.action, event.stage),
    threadId: event.thread_id ? String(event.thread_id) : null,
  }));
}

/** A share of the total, clamped so an inline bar can never overflow its track. */
export function shareOf(value, total) {
  if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0)
    return 0;
  return Math.min(1, Math.max(0, value / total));
}

// Component keys come from Cost Explorer's USAGE_TYPE (fact 28). Anything AWS
// adds later falls through to its raw key rather than rendering blank.
export const COMPONENT_LABELS = {
  runtime: "Runtime",
  memory: "Memory",
  gateway: "Gateway",
  browser: "Browser",
  code_interpreter: "Code Interpreter",
  web_search: "Web Search",
  knowledge_base: "Knowledge Base",
  evaluations: "Evaluations",
  data_transfer: "Data Transfer",
  policy: "Policy",
  other: "기타",
};

/**
 * Exact integer micro-dollars as text. `null` is never rendered as free: the
 * caller says *why* there is no figure and the string says it back.
 *
 * `unpriced` wins over `unmeasured` because it is the more actionable of the two —
 * a model missing from the rate card is fixed in code; an unmeasured turn is
 * history that cannot be recovered.
 *
 * @param {number | null | undefined} micros
 * @param {{ unpriced?: number, unmeasured?: number }} [reason]
 */
export function formatMicros(micros, reason = {}) {
  const unmeasured = reason?.unmeasured ?? 0;
  if (micros === null || micros === undefined || (micros === 0 && unmeasured > 0)) {
    if ((reason?.unpriced ?? 0) > 0) return "요율 미등록";
    return "—";
  }
  return `$${(micros / 1_000_000).toFixed(4)}`;
}

/**
 * How much of a window the model-cost figure actually covers, as one caption.
 *
 * Three ways a turn can be outside the figure, each named in the words an admin
 * can act on:
 * - `unpriced`: the model is not in the rate card (fix the card);
 * - `unsettled`: tokens were recorded but no price was written — turns the
 *   stream wrote before the ledger priced at write time; the day-close backfill
 *   prices them from the day's model;
 * - `unmeasured`: no tokens were ever recorded (history before token capture).
 *
 * `null` when every turn is in the figure, so a complete window shows nothing.
 *
 * @param {{ turns?: number, unmeasured_turns?: number, priced_turns?: number, unpriced_turns?: number }} totals
 */
export function coverageHint(totals) {
  const turns = Number(totals?.turns ?? 0);
  const unmeasured = Number(totals?.unmeasured_turns ?? 0);
  const priced = Number(totals?.priced_turns ?? 0);
  const unpriced = Number(totals?.unpriced_turns ?? 0);
  const unsettled = Math.max(0, turns - unmeasured - priced - unpriced);
  const parts = [];
  if (unpriced > 0) parts.push(`요율 미등록 ${NUMBER.format(unpriced)}턴`);
  if (unsettled > 0) parts.push(`비용 미산정 ${NUMBER.format(unsettled)}턴`);
  if (unmeasured > 0) parts.push(`토큰 기록 없음 ${NUMBER.format(unmeasured)}턴`);
  return parts.length > 0 ? `${parts.join(" · ")} 제외` : null;
}

/**
 * The bill against this page's Runtime figure, as a sentence an admin reads
 * without knowing which system produced which number.
 *
 * `diff.runtime_micros` is bill minus page. Under a tenth of a percent is
 * "일치" (Cost Explorer rounds dollars). Otherwise the sign is said in words:
 * "청구서가 7.8% 적음" — the bill is lower than this page — and the amount
 * follows in parentheses.
 */
export function billedGap(diff) {
  if (!diff || diff.runtime_micros === null || diff.runtime_micros === undefined) return "집계 대기";
  const pct = diff.runtime_pct;
  if (pct !== null && pct !== undefined && Math.abs(pct) < 0.001) return "일치";
  const dollars = `$${(Math.abs(diff.runtime_micros) / 1_000_000).toFixed(4)}`;
  const direction = diff.runtime_micros >= 0 ? "많음" : "적음";
  if (pct === null || pct === undefined) return `청구서가 ${dollars} ${direction}`;
  return `청구서가 ${(Math.abs(pct) * 100).toFixed(1)}% ${direction} (${dollars})`;
}

/** Micro-dollars as a number, for chart scales. */
export function usd(micros) {
  if (micros === null || micros === undefined) return null;
  return micros / 1_000_000;
}

/**
 * The bill against our figure, in one label.
 *
 * Under a tenth of a percent is "일치": Cost Explorer rounds dollar amounts and
 * CloudWatch quantities differ from metered usage in the last decimal, so a
 * residual that small is measurement noise, not a finding. Anything larger is
 * signed and shown with its share, so the reader sees which way and how far.
 *
 * @param {{ runtime_micros: number, runtime_pct: number | null } | null | undefined} diff
 */
export function diffLabel(diff) {
  if (!diff || diff.runtime_micros === null || diff.runtime_micros === undefined) return "집계 대기";
  const pct = diff.runtime_pct;
  if (pct !== null && pct !== undefined && Math.abs(pct) < 0.001) return "일치";
  const sign = diff.runtime_micros >= 0 ? "+" : "-";
  const dollars = `${sign}$${(Math.abs(diff.runtime_micros) / 1_000_000).toFixed(4)}`;
  if (pct === null || pct === undefined) return dollars;
  const pctText = `${pct >= 0 ? "+" : "-"}${(Math.abs(pct) * 100).toFixed(1)}%`;
  return `${dollars} (${pctText})`;
}

const COMPOSITION_LABELS = {
  model: "모델",
  runtime_active: "Runtime CPU",
  runtime_idle: "Runtime 메모리(세션 유지)",
  gateway: "Gateway",
  memory: "Memory",
};

/**
 * The cost block as stacked shares, each naming where its number came from.
 *
 * Runtime is split into CPU (billed only while consumed) and memory (billed for
 * every second a session lives, idle or not) because on this platform the memory
 * half is most of the bill and the reader should see that without a tooltip.
 * Memory uses the collector's event/retrieval figure when it has one and the
 * bill's figure otherwise — long-term storage has no metric.
 *
 * Tiers with no figure at all are left out rather than drawn as zero-width
 * segments with a legend entry that claims a value.
 */
export function costComposition(cost) {
  if (!cost) return [];
  const rows = [];
  const push = (key, micros, source) => {
    if (micros === null || micros === undefined) return;
    rows.push({ key, label: COMPOSITION_LABELS[key] ?? key, micros, source });
  };
  push("model", cost.model?.micros, "ledger");
  const runtime = cost.runtime ?? {};
  if (runtime.active_micros !== null && runtime.active_micros !== undefined && runtime.idle_micros !== null && runtime.idle_micros !== undefined) {
    push("runtime_active", runtime.active_micros, "metered");
    push("runtime_idle", runtime.idle_micros, "metered");
  } else {
    push("runtime", runtime.micros, "metered");
  }
  push("gateway", cost.gateway?.micros, "metered");
  const memory = cost.memory ?? {};
  if (memory.micros !== null && memory.micros !== undefined) push("memory", memory.micros, "metered");
  else push("memory", memory.billed_micros, "billed");
  const total = rows.reduce((sum, row) => sum + row.micros, 0);
  return rows.map((row) => ({ ...row, share: total > 0 ? row.micros / total : 0 }));
}

/** Memory's share of the runtime figure: what a keep-warm session costs. */
export function idleShare(runtime) {
  if (!runtime || runtime.micros === null || runtime.micros === undefined || !runtime.micros) return null;
  if (runtime.idle_micros === null || runtime.idle_micros === undefined) return null;
  return runtime.idle_micros / runtime.micros;
}

/**
 * Daily points reshaped for `costAnomalies`: total known cost per day in dollars.
 *
 * A day whose runtime figure is absent (not collected) is dropped rather than
 * read as $0 — an absent day is not a cheap day, and a zero baseline would flag
 * every real day as an anomaly.
 */
export function dailyCostPoints(daily) {
  return (daily ?? [])
    .filter((point) => point && point.date && point.runtime_cost_micros !== null && point.runtime_cost_micros !== undefined)
    .map((point) => ({
      date: point.date,
      cost: ((point.runtime_cost_micros ?? 0) + (point.model_cost_micros ?? 0)) / 1_000_000,
    }));
}

/**
 * Days whose cost stands out against the ones before them.
 *
 * The last day is excluded when it is `partialDay`: Cost Explorer both lags and
 * has not finished today, so today is a real 0-ish figure that must not read as an
 * anomaly. Each candidate is compared to the median of the days *before* it (a
 * median, not a mean, so one prior spike does not raise its own baseline), and
 * flagged when it exceeds `median * k`. Below `minSamples` prior days there is no
 * baseline worth judging against, so nothing is flagged.
 *
 * @param {Array<{date: string, cost: number}>} daily
 * @param {{ partialDay?: string|null, k?: number, minSamples?: number }} [opts]
 * @returns {Array<{date: string, cost: number, median: number}>}
 */
export function costAnomalies(daily, { partialDay = null, k = 2, minSamples = 3 } = {}) {
  const points = (daily || [])
    .filter((p) => p && p.date && typeof p.cost === "number")
    .filter((p) => p.date !== partialDay);
  const out = [];
  for (let i = 0; i < points.length; i += 1) {
    const prior = points.slice(0, i).map((p) => p.cost).sort((a, b) => a - b);
    if (prior.length < minSamples) continue;
    const mid = Math.floor(prior.length / 2);
    const median =
      prior.length % 2 ? prior[mid] : (prior[mid - 1] + prior[mid]) / 2;
    if (median > 0 && points[i].cost > median * k) {
      out.push({ date: points[i].date, cost: points[i].cost, median });
    }
  }
  return out;
}

/**
 * Where a rate figure came from, in the admin's words. The stored `source` is a
 * machine tag: `card:<version>` for the committed table, `bill` for a Cost
 * Explorer day the admin accepted, `price-list` for a published row,
 * `admin:<email>` for a typed entry, and the learner's own "Cost Explorer daily
 * lines …" text for what the bill taught on its own.
 */
export function rateSourceLabel(source) {
  const text = String(source ?? "");
  if (text.startsWith("card:")) return "요율표";
  if (text === "bill" || text.startsWith("Cost Explorer")) return "AWS 청구서";
  if (text === "price-list") return "Price List";
  if (text.startsWith("admin:")) return `직접 입력 · ${text.slice(6)}`;
  return text || "—";
}

/** `"4"` -> `"$4.00"`, `"1.375"` -> `"$1.375"`, `"0.2"` -> `"$0.20"`; at least two decimals, never more than four. */
export function formatUsdPer1m(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const fixed = number.toFixed(4).replace(/0+$/, "");
  const [whole, fraction = ""] = fixed.split(".");
  return `$${whole}.${fraction.padEnd(2, "0")}`;
}

/**
 * A candidate's evidence in one clause, so the admin knows what accepting it
 * asserts: "청구서 09-23~09-24 · 2일 관측" or "Price List 게시값". A single billed
 * day is offered — the learner would have waited for the second — and the count
 * says so.
 */
export function rateCandidateNote(candidate) {
  if (!candidate) return "";
  if (candidate.source === "price-list") return "Price List 게시값";
  const from = candidate.effective_from ? candidate.effective_from.slice(5) : null;
  const to = candidate.last_day ? candidate.last_day.slice(5) : null;
  const span = from && to && from !== to ? `${from}~${to}` : from ?? to ?? "";
  const days = Number(candidate.days_observed ?? 0);
  const parts = ["청구서"];
  if (span) parts.push(span);
  if (days > 0) parts.push(`${days}일 관측`);
  return parts.join(" · ");
}

/**
 * The tier cells an admin still has to fill for a row, in card order — the
 * inputs the row opens with. Complete rows open with none.
 */
export function missingTiers(row) {
  const order = ["input", "output", "cache_read", "cache_write"];
  // The long card's tiers count only where the server sent them: a family with
  // one card has no long tiers to be missing.
  const long = ["long_input", "long_output", "long_cache_read", "long_cache_write"].filter(
    (tier) => row?.tiers && tier in row.tiers,
  );
  return [...order, ...long].filter((tier) => !row?.tiers?.[tier]);
}

/**
 * The dated overlay in one clause for the bill panel: how many figures sit on
 * top of the committed card and where each kind came from, e.g.
 * "요율표에 추가된 요율 6개 (청구서 학습 2 · 직접 입력 4) · claude-opus-5-5". The earlier
 * text called every entry "청구서에서 확인해 추가한", which was false the moment an
 * admin typed one in.
 */
export function overlaySummary(entries) {
  const rows = Array.isArray(entries) ? entries : [];
  if (rows.length === 0) return null;
  const counts = { bill: 0, price: 0, admin: 0 };
  for (const row of rows) {
    const source = String(row?.source ?? "");
    if (source === "price-list") counts.price += 1;
    else if (source.startsWith("admin:")) counts.admin += 1;
    else counts.bill += 1;
  }
  const kinds = [];
  if (counts.bill > 0) kinds.push(`청구서 학습 ${counts.bill}`);
  if (counts.price > 0) kinds.push(`Price List ${counts.price}`);
  if (counts.admin > 0) kinds.push(`직접 입력 ${counts.admin}`);
  const families = [...new Set(rows.map((row) => row.family))].sort();
  return `요율표에 추가된 요율 ${rows.length}개 (${kinds.join(" · ")}) · ${families.join(", ")}`;
}
