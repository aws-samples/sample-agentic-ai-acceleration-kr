/**
 * Client for the insights API at /api/insights/*.
 *
 * Three response conventions the UI depends on:
 *
 * - `sources` says which of the independent sources answered. A false flag
 *   collapses that card; it is not an error state.
 * - Any token figure may be a floor. `unmeasured_turns > 0` means part of the
 *   window carries turns that never reported tokens, so the number next to it is
 *   "at least this much", never a total.
 * - **Every cost is an integer of micro-dollars** (`*_micros`), written by the
 *   server when the turn ended or when the collector priced a day — never
 *   estimated at read time. `null` means "no figure", and the row says why
 *   (`unpriced_turns`, `unmeasured_turns`, a collector that has not run).
 * - **`/summary` reads DynamoDB only** and is free to poll. `/telemetry` is the
 *   performance tier (latency, error rate) behind a click because CloudWatch
 *   bills per metric requested.
 */

import { authedFetch } from "./http";

// Same constant every other client in web/src/lib uses.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export interface InsightsSources {
  /** False when a month partition could not be read: totals are short. */
  usage: boolean;
  /** True when the collector has written today's runtime quantities. */
  collector: boolean;
  /** True when a reconciliation snapshot exists in the window. */
  recon: boolean;
  /** True when per-session USAGE_LOGS are being folded (per-user runtime cost). */
  usage_logs: boolean;
  guardrail?: boolean;
}

/**
 * The window descriptor every response repeats.
 *
 * `timezone` is here because a date axis whose timezone is unstated is one the
 * reader assumes is theirs, and the default is UTC — where a Korean deployment's
 * day breaks at 09:00 local. `partial_day` is the end date, always: the window
 * ends now, so its last day is still being written and anything that subtracts or
 * draws it has to know.
 */
export interface WindowFields {
  days: number;
  start_date: string;
  end_date: string;
  timezone: string;
  partial_day: string;
}

/**
 * One agent's counters and its cost, all written before the request arrived.
 *
 * The four token tiers are separate fields because Bedrock bills them at four
 * different rates. `model_cost_micros` is the ledger's sum of turn costs priced
 * at write time from the repository rate card; `runtime_cost_micros` is the
 * collector's CloudWatch quantities × the published rate; `billed_runtime_micros`
 * is what Cost Explorer says for the same agent (24–48h behind) and
 * `cost_diff_micros` the gap. `null` on any of them is a stated absence, and
 * `priced_turns` / `unpriced_turns` say how much of the window the model figure
 * covers.
 */
export interface AgentUsageRow {
  record_id: string;
  name: string;
  /** Every model this agent ran on in the window, as recorded per turn. */
  model_ids: string[];
  turns: number;
  distinct_users: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  unmeasured_turns: number;
  tool_calls: number;
  interrupted_turns: number;
  /** Turns that died on a model or runtime error. Distinct from interrupted. */
  failed_turns: number;
  threads_started: number;
  priced_turns: number;
  unpriced_turns: number;
  model_cost_micros: number | null;
  runtime_cost_micros: number | null;
  total_cost_micros: number | null;
  /** Runtime invocations the collector counted (includes keep-warm pings). */
  invocations: number | null;
  sessions: number | null;
  billed_runtime_micros: number | null;
  cost_diff_micros: number | null;
  /** Turns whose model calls carried a guardrail trace. 0 with turns > 0 = unguarded. */
  guardrail_scanned_turns?: number;
}

/**
 * What CloudWatch says about one agent, once a reader has asked for it.
 *
 * Every field is nullable and an agent CloudWatch has no series for is **not in
 * the list at all**, so "unmeasured" stays distinguishable from "idle".
 * `error_rate` is null unless its numerator and denominator came from the same
 * discovered series — a ratio across two dimension widths compares two
 * populations and used to read over 100%.
 */
export interface TelemetryRow {
  record_id: string;
  name: string;
  invocations: number | null;
  latency_p90_ms: number | null;
  error_rate: number | null;
  /**
   * Which of `SystemErrors`/`UserErrors` the rate's numerator holds. One entry means
   * the other series does not exist for this agent, so the rate covers half the
   * failure modes — rendered with a `*`. Absent on an older payload.
   */
  error_basis?: string[] | null;
  vcpu_hours: number | null;
  gb_hours: number | null;
}

/** The performance tier. No cost lives here: that is the collector's, on `/summary`. */
export interface Telemetry extends WindowFields {
  sources: { cloudwatch: boolean };
  agents: TelemetryRow[];
  totals: {
    vcpu_hours: number;
    gb_hours: number;
    /**
     * Rows can outnumber runtimes: two registry records may point at one harness,
     * and their rows then carry identical vended figures because they describe the
     * same physical runtime. The totals count runtimes, so this is what explains
     * why they are not the column sums.
     */
    agents_listed: number;
    distinct_runtimes: number;
  };
}

/**
 * A leaderboard row: our counters, plus the vended fields if they were fetched.
 *
 * The CloudWatch half is optional rather than nullable, so a column can tell
 * "not asked for yet" from "asked, and CloudWatch had nothing" — both render as
 * a dash, but only the second one is a fact about the agent.
 */
export type LeaderboardRow = AgentUsageRow & Partial<Omit<TelemetryRow, "record_id" | "name">>;

export interface DailyPoint {
  date: string;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  failed_turns: number;
  unmeasured_turns: number;
  /** Ledger cost written for the day; 0 on a day with no priced turns. */
  model_cost_micros?: number;
  /** Collector cost for the day; null when the collector has no item for it. */
  runtime_cost_micros?: number | null;
  tokens_known: boolean;
  /** Guardrail interventions that day. A count like turns: zero is a real zero. */
  guardrail_interventions?: number;
  /**
   * True for a day the window covers that had no items at all — no turns, so every
   * counter is a real zero. The series is dense over the window because consumers
   * that count rows (the half-window delta, the axis) are otherwise wrong about
   * time: four rows across seven days compared two days five days apart.
   */
  filled?: boolean;
}

/**
 * Guardrail counters for one agent, one user, or the totals.
 *
 * Counts only — the matched text never leaves Bedrock's trace. `by_filter` names
 * the filter that fired (`INSULTS`, `PROMPT_ATTACK`, a PII type, a topic), which
 * is what separates a jailbreak attempt from a rude user when both are "content".
 * `by_confidence` is the content filters' own certainty; a run of LOW-confidence
 * blocks is the one signal that a filter strength is set too high.
 *
 * `scanned_turns` counts turns that carried a guardrail trace at all. Zero
 * interventions on a guarded agent and zero on an unguarded one are the same
 * number; this is what tells them apart.
 */
export interface GuardrailCounters {
  interventions: number;
  blocked_input: number;
  blocked_output: number;
  anonymized_input: number;
  anonymized_output: number;
  policy_content: number;
  policy_pii: number;
  policy_topic: number;
  policy_word: number;
  scanned_turns: number;
  by_filter: Record<string, number>;
  by_confidence: Record<string, number>;
  /** interventions / turns over the window; null when the agent had no turns. */
  intervention_rate?: number | null;
}

/** One intervention: a pointer at the turn, never the text that tripped it. */
export interface GuardrailEvent {
  at: string;
  agent_record_id: string;
  owner_sub: string;
  thread_id: string;
  turn_id: string;
  action: string;
  stage: string;
  policies: string[];
  filter_types: string[];
  confidences: string[];
}

export interface GuardrailSummary {
  by_agent: Record<string, GuardrailCounters>;
  totals: GuardrailCounters;
  /** Record ids that served turns in the window without a single guardrail scan. */
  unguarded_agents: string[];
  /** Newest first, capped by the server. Admin-only route, so subs are present. */
  recent?: GuardrailEvent[];
  by_user?: Record<string, GuardrailCounters>;
}

/** The window's counters, every one of them exact. */
export interface InsightsTotals {
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  unmeasured_turns: number;
  tool_calls: number;
  interrupted_turns: number;
  failed_turns: number;
  priced_turns: number;
  unpriced_turns: number;
}

export interface ModelCost {
  /** Sum of every priced turn's cost in the window; null when none was priced. */
  micros: number | null;
  priced_turns: number;
  unpriced_turns: number;
  /** Model ids seen in the window that the rate card does not know. */
  unregistered_models: string[];
  rate_card_version: string;
}

export interface RuntimeCost {
  micros: number | null;
  vcpu_hours_micro: number | null;
  gb_hours_micro: number | null;
  /** CPU half: billed only while consumed. */
  active_micros: number | null;
  /** Memory half: billed for every second a session lived, idle included. */
  idle_micros: number | null;
  /** Newest `collected_at` among the items read; how stale the figure is. */
  as_of: string | null;
  /** The last day the collector marked complete. */
  complete_through: string | null;
  runtimes: number;
  /** Per-session (USAGE_LOGS) runtime cost in the window; null until sessions flow. */
  session_micros?: number | null;
  /** The share of `session_micros` spent by keep-warm pings — no conversation caused it. */
  keepwarm_micros?: number | null;
  /**
   * Runtime cost no leaderboard row claims: an MCP-app record's runtime, a
   * runtime a redeploy retired, a deleted harness's companion. In `micros`,
   * in no agent row — null when every collected runtime has a row.
   */
  unclaimed_micros?: number | null;
  unclaimed_runtimes?: { runtime: string; runtime_cost_micros: number }[];
}

export interface RateMismatch {
  family: string;
  routing: string;
  tier: string;
  card_micro: number | null;
  billed_micro: number | null;
}

/** A dated entry laid over the committed card: learned from the bill, fetched from the Price List, or entered by an admin. */
export interface LearnedRate {
  family: string;
  routing: string;
  tier: string;
  effective_from: string;
  usd_per_1m: string;
  /** `bill`, `price-list`, `admin:<email>`, or the learner's own text. Absent on an older server. */
  source?: string;
}

export interface RateCardStatus {
  version: string;
  checked_at: string | null;
  mismatches: RateMismatch[];
  /** Families the bill carries that the card does not price — yet. */
  unregistered_families: string[];
  /** Dated entries learned from the bill, oldest first. Absent on an older server. */
  learned?: LearnedRate[];
}

/** One (family, routing) the platform ran, each tier with its figure and where it came from. */
export interface RateTier {
  usd_per_1m: string;
  /** `card:<version>`, `bill`, `price-list`, `admin:<email>` or the learner's own text. */
  source: string;
  effective_from: string | null;
}

export interface RateRow {
  family: string;
  routing: "global" | "regional" | string;
  models: string[];
  turns: number;
  unpriced_turns: number;
  unpriced_since: string | null;
  tiers: Record<string, RateTier | null>;
  complete: boolean;
  /** Prompt size above which a call is billed on the family's long card (`long_*` tiers); null when it has one card. */
  long_context_threshold?: number | null;
}

/** A figure a source can state that the card lacks or disputes. */
export interface RateCandidate {
  family: string;
  routing: string;
  tier: string;
  usd_per_1m: string;
  source: "bill" | "price-list" | string;
  /** The bill's: first day of the trailing run at this value. Absent on Price List rows. */
  effective_from?: string;
  last_day?: string;
  days_observed?: number;
  current_usd_per_1m: string | null;
  usage_type?: string;
}

export interface RateEntry {
  key: string;
  family: string;
  routing: string;
  tier: string;
  effective_from: string;
  usd_per_1m: string;
  source: string;
  registered_at: string | null;
}

export interface RateCardOverview {
  window: { days: number; start_date: string; end_date: string; timezone: string; partial_day: string };
  version: string;
  rows: RateRow[];
  candidates: RateCandidate[];
  entries: RateEntry[];
}

export interface RateEntryInput {
  family: string;
  routing: string;
  tier: string;
  usd_per_1m: string;
  effective_from: string;
  source?: string;
}

export interface RepriceReport {
  repriced: number;
  newly_priced: number;
  still_unpriced: number;
  unpriced: number;
}

export interface BilledSnapshot {
  runtime_micros: number | null;
  component: Record<string, number>;
  latest_day: string | null;
  ce_estimated: boolean;
  checked_at: string | null;
}

/**
 * Bill minus this page, over complete days only: the window's last day is
 * still being written and Cost Explorer has only part of it, so it is never in
 * the comparison. `through` is the last day that was.
 */
export interface CostDiff {
  runtime_micros: number;
  runtime_pct: number | null;
  days_compared: number;
  through?: string;
}

/**
 * The page's cost section. Ledger and collector are the figures; the bill is the
 * check. Nothing here is estimated at read time.
 */
export interface CostBlock {
  model: ModelCost;
  runtime: RuntimeCost;
  /** `invocations` is every MCP request; `billable_requests` the tool ones the bill counts (null before the split). */
  gateway: { micros: number | null; invocations: number; billable_requests?: number | null };
  memory: { micros: number | null; events: number; retrievals: number; billed_micros: number | null };
  total_micros: number | null;
  billed: BilledSnapshot | null;
  diff: CostDiff | null;
  rate_card: RateCardStatus;
}

export interface ModelRow {
  model_id: string;
  family: string | null;
  routing: string | null;
  registered: boolean;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  model_cost_micros: number | null;
  agents: number;
}

export interface InsightsSummary extends WindowFields {
  sources: InsightsSources;
  totals: InsightsTotals;
  /** Ledger + collector figures, with the bill as the check. */
  cost: CostBlock;
  agents: AgentUsageRow[];
  /** One row per model across agents, for the mix panel. */
  models: ModelRow[];
  daily: DailyPoint[];
  guardrail?: GuardrailSummary | null;
  /**
   * sub -> email for the people the guardrail section names. The pool signs in by
   * email, so a sub is a UUID and the token carries no name; the server resolves
   * it in Cognito at read time. A sub the directory could not name is absent.
   */
  subjects?: Record<string, string>;
}

export interface ToolUsage {
  name: string;
  tool_calls: number;
  metric: string;
}

/** One turn event from the ledger, newest first in `RecordInsights.turns`. */
export interface RecordTurn {
  turn_id: string;
  thread_id: string;
  ended_at: string;
  status: "completed" | "interrupted" | "failed" | "unknown";
  model_id: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  model_cost_micros: number | null;
  measured: boolean;
  source: "stream" | "backfill";
  owner_sub: string | null;
}

export interface RecordInsights extends WindowFields {
  record_id: string;
  /** False when a month partition could not be read, so the totals are short. */
  sources?: { usage: boolean };
  totals: Record<string, number>;
  /** The model the agent runs now (header only; cost never reads it). */
  model_id: string | null;
  models: Array<{
    model_id: string;
    turns: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    model_cost_micros: number | null;
    registered: boolean;
  }>;
  model_cost_micros: number | null;
  runtime_cost_micros: number | null;
  invocations: number | null;
  turns: RecordTurn[];
  distinct_users: number;
  daily: DailyPoint[];
  tools: ToolUsage[];
  /**
   * Set for every non-agent record (skill, MCP server, gateway), whose own
   * turn/token counters are always zero because it is reached rather than run.
   * When present, the Usage view shows the agents that reach it and their
   * traffic instead of those empty counters. `metric` is `traffic` when the
   * turns passed through the record (an MCP call) and `reach` when they merely
   * ran on an agent that attaches it (a skill, which nothing counts being read).
   * `agent_records` ranks those agents by traffic; evaluation lives on them.
   */
  reach?: RecordReach | null;
}

export interface RecordReach {
  metric: "traffic" | "reach";
  agents: number;
  turns: number;
  /** The agents that reach this record, ranked by their turns in the window. */
  agent_records: Array<{ record_id: string; name: string | null; turns: number }>;
  /**
   * The same turns and calls per day, dense over the window, oldest first. For
   * a skill `tool_calls` is always zero — nothing counts a skill being read.
   */
  daily: Array<{ date: string; turns: number; tool_calls: number }>;
}

export interface CompositionEntry {
  record_id: string;
  name: string;
  turns: number;
  agents: number;
  metric: string;
}

export interface Composition extends WindowFields {
  mcp: CompositionEntry[];
  skills: CompositionEntry[];
  knowledge_bases: CompositionEntry[];
  tools: ToolUsage[];
}

export interface MyUsage extends WindowFields {
  sub: string;
  totals: Record<string, number>;
}

/**
 * One person's usage and spend. Keyed by Cognito `sub`; the name, when the server
 * could resolve one, is in the response's `subjects` map.
 *
 * `model_cost_micros` is the ledger's own sum: each turn was priced by the model
 * it ran on and the cost rode onto the user's day item. `runtime_cost_micros`
 * exists only when per-session USAGE_LOGS attribution is on; without it the
 * column is absent, never zero.
 */
export interface UserUsageRow {
  sub: string;
  /** The agents this person used in the window, most-used first. */
  agents?: { record_id: string; name: string; turns: number }[];
  /** Days in the window on which this person took at least one turn. */
  active_days?: number;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  failed_turns: number;
  /** Conversations this person started in the window; turns per thread is depth. */
  threads_started?: number;
  /** Turns this person abandoned mid-stream (distinct from `failed_turns`). */
  interrupted_turns?: number;
  /** Tool calls the agents made on this person's behalf. */
  tool_calls?: number;
  unmeasured_turns: number;
  priced_turns: number;
  unpriced_turns: number;
  model_cost_micros: number | null;
  runtime_cost_micros: number | null;
  total_cost_micros: number | null;
  /** Guardrail interventions attributed to this person, when the server sends them. */
  guardrail_interventions?: number;
}

export interface UserLeaderboard extends WindowFields {
  /** `usage_logs_since`: the first day a session was logged in the window; per-person runtime cost starts there. */
  sources: { usage: boolean; usage_logs: boolean; usage_logs_since?: string | null };
  users: UserUsageRow[];
  /** Distinct people per day, dense over the window (an idle day is 0). */
  daily_active_users?: { date: string; users: number }[];
  /** sub -> email for the rows that resolve; see `InsightsSummary.subjects`. */
  subjects?: Record<string, string>;
  totals: {
    users: number;
    turns: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    failed_turns: number;
    threads_started?: number;
    interrupted_turns?: number;
    /** Endings the agent ledger counted on days when no person's row recorded them. */
    interrupted_turns_unattributed?: number;
    failed_turns_unattributed?: number;
    tool_calls?: number;
    unmeasured_turns: number;
    priced_turns: number;
    unpriced_turns: number;
    model_cost_micros: number | null;
    runtime_cost_micros: number | null;
    guardrail_interventions?: number;
  };
}

export class InsightsApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "InsightsApiError";
    this.status = status;
  }
}

async function request<T>(endpoint: string): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`);
  if (!response.ok) {
    const err = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new InsightsApiError(
      response.status,
      err.detail || `HTTP ${response.status}`,
    );
  }
  return response.json();
}

export function fetchSummary(days: number): Promise<InsightsSummary> {
  return request<InsightsSummary>(`/api/insights/summary?days=${days}`);
}

/**
 * The vended tier. Call this from a click, never from the poll.
 *
 * $0.01 per 1,000 metrics requested, measured at 72 metrics a sweep, cached for
 * five minutes on the server — so pressing the button twice is free and leaving the
 * page open costs nothing at all.
 */
export function fetchTelemetry(days: number): Promise<Telemetry> {
  return request<Telemetry>(`/api/insights/telemetry?days=${days}`);
}

export function fetchRecordInsights(
  recordId: string,
  days: number,
): Promise<RecordInsights> {
  return request<RecordInsights>(
    `/api/insights/records/${encodeURIComponent(recordId)}?days=${days}`,
  );
}

export function fetchComposition(days: number): Promise<Composition> {
  return request<Composition>(`/api/insights/composition?days=${days}`);
}

export function fetchMyUsage(days: number): Promise<MyUsage> {
  return request<MyUsage>(`/api/insights/me?days=${days}`);
}

/**
 * Usage per person. **403 for a plain user** — the aggregates on this page name
 * agents, which are public records, and this one names people.
 *
 * The widget renders the 403 as "관리자만" rather than as an error, because for a
 * non-admin it is the correct answer rather than a failure.
 */
export function fetchUserLeaderboard(days: number): Promise<UserLeaderboard> {
  return request<UserLeaderboard>(`/api/insights/users?days=${days}`);
}

export interface TeamUsageRow {
  team: string;
  label: string;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  failed_turns: number;
  tool_calls: number;
  unmeasured_turns: number;
  priced_turns: number;
  unpriced_turns: number;
  model_cost_micros: number;
  policy_denials: number;
  denied_tools: Record<string, number>;
  daily_cost_alert_usd: number | null;
}

export interface TeamInsights extends WindowFields {
  sources: { usage: boolean; policy_metrics: boolean };
  teams: TeamUsageRow[];
  unattributed: { turns: number; model_cost_micros: number; policy_denials: number };
  gateway_decisions: {
    allow: number;
    deny: number;
    by_tool: Record<string, number>;
    /** LOG_ONLY counts what would have been denied; only ENFORCE blocked calls. */
    by_mode?: Record<string, { allow: number; deny: number }>;
  };
  recent_denials: { at: string; team: string; tool_name: string; agent_record_id: string; thread_id: string }[];
}

/** Per-team spend and policy denials. 403 for a plain user, rendered as "관리자만". */
export function fetchTeamInsights(days: number): Promise<TeamInsights> {
  return request<TeamInsights>(`/api/insights/teams?days=${days}`);
}

export interface TraceSpan {
  name: string;
  span_id: string | null;
  parent_span_id: string | null;
  start_time: string | null;
  duration_ms: number | null;
}

/**
 * A thread's span timeline.
 *
 * Thread-scoped, not turn-scoped: the server asks Logs Insights for every span in
 * the session over the thread's lifetime. The route used to carry a turn id that
 * it echoed back without filtering on, so the chat drew the same thread-wide
 * timeline under every turn and labelled each one "이 턴".
 */
export interface ThreadTraces {
  thread_id: string;
  status: string;
  spans: TraceSpan[];
  log_group?: string | null;
  sources: { traces: boolean };
  detail?: string;
}

export function fetchThreadTraces(threadId: string): Promise<ThreadTraces> {
  return request<ThreadTraces>(
    `/api/insights/traces/${encodeURIComponent(threadId)}`,
  );
}

export interface RecordThread {
  thread_id: string;
  created_at: string | null;
  updated_at: string | null;
  status: string | null;
  turns: number;
  /** The thread's opening question, one line, or "" when it had no readable text. */
  title: string;
  /**
   * The Cognito sub of the thread's owner; the response's `subjects` map names it
   * when the server could. Only present for an admin, who alone sees other
   * people's threads; null for a plain user, whose list is already all their own.
   */
  owner_sub: string | null;
}

/**
 * The threads one agent answered in — how the drill-down gets from an agent to
 * something a trace or an evaluation can act on. Owner-scoped for a plain user,
 * everyone's for an admin, so an empty list is a normal answer.
 */
export function fetchRecordThreads(
  recordId: string,
  limit = 20,
): Promise<{ record_id: string; threads: RecordThread[]; subjects?: Record<string, string> }> {
  return request(
    `/api/insights/records/${encodeURIComponent(recordId)}/threads?limit=${limit}`,
  );
}

export interface Evaluator {
  evaluator_id: string;
  name: string;
  level: string;
  builtin: boolean;
  status: string;
}

export interface EvaluationScore {
  evaluator_id: string;
  average_score: number | null;
  evaluated: number;
  failed: number;
}

export interface EvaluationSessions {
  total: number;
  completed: number;
  failed: number;
  in_progress: number;
  ignored: number;
}

/**
 * Every status a batch evaluation can report, plus the two this platform adds.
 *
 * A literal union rather than `string`, so that `status !== "none"` narrows
 * `EvaluationRun | {status: "none"}` on its own. With `string` it cannot — `string`
 * includes `"none"` — and every use site then needs an `as EvaluationRun` cast,
 * which is a cast standing in for a guard.
 *
 * `"unavailable"` is ours: the status route degrades inside a 200 so the UI can
 * offer a retry instead of having SWR back off.
 */
export type EvaluationStatus =
  | "PENDING"
  | "IN_PROGRESS"
  | "COMPLETED"
  | "COMPLETED_WITH_ERRORS"
  | "FAILED"
  | "STOPPING"
  | "STOPPED"
  | "DELETING"
  | "unavailable";

export interface EvaluationRun {
  batch_id: string;
  name: string;
  status: EvaluationStatus;
  terminal: boolean;
  sessions: EvaluationSessions;
  scores: EvaluationScore[];
  /** `errorDetails` from AgentCore — sentences like "1 of 28 sessions failed". */
  errors: string[];
  created_at: string;
  result_log_group?: string;
  sources?: { evaluations: boolean };
}

export interface EvaluatorsResponse {
  evaluators: Evaluator[];
  sources: { evaluations: boolean };
}

/**
 * AgentCore insights — the triage run that shares the batch machinery with an
 * evaluation but answers with clustered trees instead of scores.
 *
 * Every cluster carries AgentCore's own `affected_session_count`; the client
 * never sums them (one session can sit in several categories). The `sessions`
 * lists beneath quote the conversation — `user_messages` is the user's words —
 * so the server empties them for a plain user and says so with
 * `session_details: false`. Read that flag, not the list length: an empty list
 * under a count of 3 is "not for you", not "nobody".
 */
export interface InsightSessionHit {
  session_id: string;
  /** Resolved from the record's threads for an admin; null when no thread matched. */
  thread_id?: string | null;
  explanation?: string | null;
  fix_type?: string | null;
  recommendation?: string | null;
  user_messages?: string[];
  approach_taken?: string | null;
  final_outcome?: string | null;
}

export interface InsightCluster {
  cluster_id: number | null;
  name: string;
  description: string | null;
  affected_session_count: number;
  sessions: InsightSessionHit[];
}

export interface InsightRootCause extends InsightCluster {
  root_cause: string | null;
  recommendation: string | null;
}

export interface InsightSubCategory {
  cluster_id: number | null;
  name: string;
  description: string | null;
  affected_session_count: number;
  root_causes: InsightRootCause[];
}

export interface InsightFailureCategory {
  cluster_id: number | null;
  name: string;
  description: string | null;
  affected_session_count: number;
  sub_categories: InsightSubCategory[];
}

export interface InsightsTree {
  /** The insight ids the run asked for — how "no failures" is told from "not asked". */
  requested: string[];
  session_details: boolean;
  failures: InsightFailureCategory[];
  user_intents: InsightCluster[];
  execution_summaries: InsightCluster[];
}

/** An insights run. `insights` is null until the run completes. */
export interface AnalysisRun extends EvaluationRun {
  insights: InsightsTree | null;
}

/**
 * A body-carrying request. The method is a parameter rather than hardcoded,
 * because it was not: the layout save was sent as `POST` against a `PUT` route,
 * which 405s. The write is deliberately optimistic and never reverts the user's
 * arrangement, so the only symptom was the layout quietly reappearing in its old
 * order after a reload — measured 2026-08-16 on the deployed stack.
 */
async function send<T>(
  method: "POST" | "PUT" | "DELETE",
  endpoint: string,
  body?: unknown,
): Promise<T> {
  const response = await authedFetch(`${API_BASE}${endpoint}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const err = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new InsightsApiError(
      response.status,
      err.detail || `HTTP ${response.status}`,
    );
  }
  return response.json();
}

function post<T>(endpoint: string, body: unknown): Promise<T> {
  return send<T>("POST", endpoint, body);
}

function put<T>(endpoint: string, body: unknown): Promise<T> {
  return send<T>("PUT", endpoint, body);
}

export function fetchEvaluators(): Promise<EvaluatorsResponse> {
  return request<EvaluatorsResponse>("/api/insights/evaluators");
}

export function startEvaluation(
  threadIds: string[],
  evaluatorIds: string[],
  recordId?: string,
): Promise<EvaluationRun> {
  return post<EvaluationRun>("/api/insights/evaluations", {
    thread_ids: threadIds,
    evaluator_ids: evaluatorIds,
    record_id: recordId,
  });
}

export function fetchEvaluation(batchId: string): Promise<EvaluationRun> {
  return request<EvaluationRun>(
    `/api/insights/evaluations/${encodeURIComponent(batchId)}`,
  );
}

export function fetchRecordEvaluation(
  recordId: string,
): Promise<EvaluationRun | { status: "none" }> {
  return request<EvaluationRun | { status: "none" }>(
    `/api/insights/records/${encodeURIComponent(recordId)}/evaluation`,
  );
}

export function startAnalysis(
  threadIds: string[],
  recordId?: string,
): Promise<AnalysisRun> {
  return post<AnalysisRun>("/api/insights/analyses", {
    thread_ids: threadIds,
    record_id: recordId,
  });
}

export function fetchAnalysis(batchId: string): Promise<AnalysisRun> {
  return request<AnalysisRun>(
    `/api/insights/analyses/${encodeURIComponent(batchId)}`,
  );
}

export function fetchRecordAnalysis(
  recordId: string,
): Promise<AnalysisRun | { status: "none" }> {
  return request<AnalysisRun | { status: "none" }>(
    `/api/insights/records/${encodeURIComponent(recordId)}/analysis`,
  );
}

export interface LayoutResponse {
  version: number;
  widgets: Array<{
    id: string;
    span: string;
    visible: boolean;
  }>;
  persisted: boolean;
}

/**
 * The model rate card as the admin manages it. `fetchRateCard` reads DDB and the
 * process overlay only; `fetchPublishedRates` is the one live call (Price List)
 * and is made from a button. Writes reprice on the server before returning, so
 * the caller refetches the summary right after.
 */
export function fetchRateCard(days: number): Promise<RateCardOverview> {
  return request<RateCardOverview>(`/api/insights/rates?days=${days}`);
}

export function fetchPublishedRates(): Promise<{ published: number; candidates: RateCandidate[] }> {
  return post(`/api/insights/rates/fetch`, {});
}

export function putRates(
  entries: RateEntryInput[],
): Promise<{ saved: string[]; repriced: RepriceReport }> {
  return put(`/api/insights/rates`, { entries });
}

export function deleteRate(key: string): Promise<{ removed: string; repriced: RepriceReport }> {
  return send("DELETE", `/api/insights/rates?key=${encodeURIComponent(key)}`);
}

export function fetchLayout(): Promise<LayoutResponse> {
  return request<LayoutResponse>("/api/insights/layout");
}

export function putLayout(layout: {
  version?: number;
  widgets?: Array<{
    id: string;
    span: string;
    visible: boolean;
  }>;
}): Promise<LayoutResponse> {
  return put<LayoutResponse>("/api/insights/layout", layout);
}
