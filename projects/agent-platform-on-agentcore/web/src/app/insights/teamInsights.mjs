/**
 * The Organization view of `/api/insights/teams`, as chart rows. Pure, so the
 * arithmetic the two widgets (팀별 사용량, 게이트웨이 정책) draw is pinned by
 * teamInsights.test.mjs rather than by squinting at a bar.
 *
 * Money stays in micros until the last step: a chart axis wants dollars, but a
 * share or a per-member figure computed from rounded dollars drifts.
 */
import { shortDate } from "./insightsFormat.mjs";

/** The slot the unattributed remainder is stacked under. Not a team name: `-` cannot be one. */
export const UNATTRIBUTED_KEY = "-";

/** How many teams get their own colour before the rest fold into "기타 팀". */
export const TEAM_SERIES_CAP = 7;

const toUsd = (micros) => Math.round(Number(micros || 0)) / 1_000_000;

/**
 * The teams as chart series, in the server's order (which is the team list's,
 * not this window's ranking) so a team keeps its colour when a window reorders
 * them. Past the cap the tail is one "기타 팀" series.
 *
 * @param {Array<{team: string, label?: string}>} teams
 * @returns {Array<{key: string, name: string, colorIndex: number | null, members: string[]}>}
 */
export function teamSeries(teams) {
  const list = teams ?? [];
  const head = list.slice(0, TEAM_SERIES_CAP).map((t, i) => ({
    key: t.team,
    name: t.label || t.team,
    colorIndex: i,
    members: [t.team],
  }));
  if (list.length > TEAM_SERIES_CAP) {
    head.push({
      key: "__other",
      name: `기타 팀 ${list.length - TEAM_SERIES_CAP}개`,
      colorIndex: TEAM_SERIES_CAP,
      members: list.slice(TEAM_SERIES_CAP).map((t) => t.team),
    });
  }
  return head;
}

/**
 * One row per day, one column per series plus the unattributed remainder.
 *
 * @param {Array<{team: string, daily: Array<Record<string, any>>}>} teams
 * @param {{daily?: Array<Record<string, any>>}} unattributed
 * @param {string} field  `model_cost_micros` (drawn in dollars), `turns` or `policy_denials`
 * @param {string | null} [partialDay]
 */
export function teamDailyRows(teams, unattributed, field, partialDay = null) {
  const series = teamSeries(teams);
  const owner = new Map();
  for (const s of series) for (const name of s.members) owner.set(name, s.key);
  const money = field === "model_cost_micros";
  const read = (point) => (money ? toUsd(point?.[field]) : Number(point?.[field] || 0));

  const rows = new Map();
  const rowFor = (date) => {
    if (!rows.has(date)) {
      const row = {
        date,
        label: shortDate(date) + (partialDay && date === partialDay ? "*" : ""),
        [UNATTRIBUTED_KEY]: 0,
      };
      for (const s of series) row[s.key] = 0;
      rows.set(date, row);
    }
    return rows.get(date);
  };
  for (const team of teams ?? []) {
    const key = owner.get(team.team);
    for (const point of team.daily ?? []) {
      const row = rowFor(point.date);
      row[key] = round6(row[key] + read(point));
    }
  }
  for (const point of unattributed?.daily ?? []) {
    const row = rowFor(point.date);
    row[UNATTRIBUTED_KEY] = round6(row[UNATTRIBUTED_KEY] + read(point));
  }
  return [...rows.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
}

const round6 = (n) => Math.round(n * 1_000_000) / 1_000_000;

/**
 * The four figures the usage widget opens with. `null` for a ratio whose
 * denominator is zero — "0% 귀속" over no spend would claim a measurement.
 *
 * @param {{teams: Array<Record<string, any>>, unattributed: Record<string, any>}} data
 */
export function teamHeadline(data) {
  const teams = data?.teams ?? [];
  const sum = (key) => teams.reduce((n, t) => n + Number(t[key] || 0), 0);
  const attributed = sum("model_cost_micros");
  const remainder = Number(data?.unattributed?.model_cost_micros || 0);
  const turns = sum("turns");
  const failed = sum("failed_turns");
  return {
    attributedCostMicros: attributed,
    attributedShare: attributed + remainder > 0 ? attributed / (attributed + remainder) : null,
    turns,
    failureRate: turns > 0 ? failed / turns : null,
    teamsWithAlert: teams.filter((t) => t.daily_cost_alert_usd).length,
    teamsOverAlert: teams.filter((t) => (t.days_over_alert ?? 0) > 0).length,
  };
}

/**
 * A per-head figure, or null when the head count is unknown or zero.
 *
 * @param {number} value
 * @param {number | null | undefined} members
 */
export function perMember(value, members) {
  if (members === null || members === undefined || members <= 0) return null;
  return Number(value || 0) / members;
}

/**
 * One team's daily cost against its alert, in dollars, for a small multiple.
 *
 * @param {{daily: Array<Record<string, any>>, daily_cost_alert_usd: number | null}} team
 * @param {string | null} [partialDay]
 */
export function alertSeries(team, partialDay = null) {
  const alert = team?.daily_cost_alert_usd || null;
  return (team?.daily ?? []).map((p) => {
    const usd = toUsd(p.model_cost_micros);
    return {
      date: p.date,
      label: shortDate(p.date) + (partialDay && p.date === partialDay ? "*" : ""),
      cost: usd,
      over: alert !== null && usd >= alert,
    };
  });
}

/**
 * Gateway decisions per day, the deny side split by mode.
 *
 * ENFORCE denials were blocked; LOG_ONLY "denials" passed and only say what a
 * policy would have done. They are different facts and never one stack total.
 *
 * @param {Array<{date: string, by_mode: Record<string, {allow: number, deny: number}>}>} daily
 * @param {string | null} [partialDay]
 */
export function decisionRows(daily, partialDay = null) {
  return (daily ?? []).map(({ date, by_mode }) => {
    const enforce = by_mode?.ENFORCE ?? { allow: 0, deny: 0 };
    const logOnly = by_mode?.LOG_ONLY ?? { allow: 0, deny: 0 };
    return {
      date,
      label: shortDate(date) + (partialDay && date === partialDay ? "*" : ""),
      enforce_allow: Number(enforce.allow || 0),
      enforce_deny: Number(enforce.deny || 0),
      log_only_deny: Number(logOnly.deny || 0),
    };
  });
}

/** `t___lookup_salary` → `lookup_salary`: the gateway prefixes the target. */
export function toolLabel(name) {
  const s = String(name ?? "");
  return s.includes("___") ? s.slice(s.lastIndexOf("___") + 3) : s;
}

/**
 * Team × tool denial counts for the heatmap. Tools ordered by total, most
 * denied first; teams keep the server's order. `max` is the hottest cell, the
 * scale every cell's tint is read against.
 *
 * @param {Array<{team: string, label?: string, denied_tools: Record<string, number>}>} teams
 */
export function denialMatrix(teams) {
  const totals = new Map();
  for (const t of teams ?? []) {
    for (const [tool, n] of Object.entries(t.denied_tools ?? {})) {
      totals.set(tool, (totals.get(tool) ?? 0) + Number(n || 0));
    }
  }
  const tools = [...totals.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([tool]) => tool);
  let max = 0;
  const rows = (teams ?? []).map((t) => {
    const cells = tools.map((tool) => Number(t.denied_tools?.[tool] || 0));
    for (const c of cells) max = Math.max(max, c);
    return { team: t.team, name: t.label || t.team, cells, total: cells.reduce((a, b) => a + b, 0) };
  });
  return { tools, rows, max, toolTotals: tools.map((tool) => totals.get(tool)) };
}
