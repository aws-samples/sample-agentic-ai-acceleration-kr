import assert from "node:assert/strict";
import { test } from "node:test";

import {
  alertSeries,
  decisionRows,
  denialMatrix,
  perMember,
  teamDailyRows,
  teamHeadline,
  teamSeries,
  TEAM_SERIES_CAP,
  UNATTRIBUTED_KEY,
} from "./teamInsights.mjs";

const day = (date, extra = {}) => ({ date, turns: 0, model_cost_micros: 0, failed_turns: 0, tool_calls: 0, policy_denials: 0, ...extra });

test("series keep the server's order and fold the tail past the cap", () => {
  const teams = Array.from({ length: TEAM_SERIES_CAP + 2 }, (_, i) => ({ team: `t${i}`, label: i === 0 ? "재무" : "" }));
  const series = teamSeries(teams);
  assert.equal(series.length, TEAM_SERIES_CAP + 1);
  assert.equal(series[0].name, "재무");
  assert.equal(series[1].name, "t1");
  assert.deepEqual(series.at(-1).members, [`t${TEAM_SERIES_CAP}`, `t${TEAM_SERIES_CAP + 1}`]);
});

test("daily rows stack teams and the remainder, cost in dollars, today starred", () => {
  const teams = [
    { team: "fin", daily: [day("2026-10-09", { model_cost_micros: 1_500_000 }), day("2026-10-10", { model_cost_micros: 250_000 })] },
    { team: "hr", daily: [day("2026-10-09"), day("2026-10-10", { model_cost_micros: 1_000_000 })] },
  ];
  const unattributed = { daily: [{ date: "2026-10-09", turns: 1, model_cost_micros: 500_000, policy_denials: 0 }, { date: "2026-10-10", turns: 0, model_cost_micros: 0, policy_denials: 0 }] };
  const rows = teamDailyRows(teams, unattributed, "model_cost_micros", "2026-10-10");
  assert.deepEqual(rows.map((r) => r.label), ["10/9", "10/10*"]);
  assert.equal(rows[0].fin, 1.5);
  assert.equal(rows[0][UNATTRIBUTED_KEY], 0.5);
  assert.equal(rows[1].hr, 1);
  const turns = teamDailyRows(teams, unattributed, "turns");
  assert.equal(turns[0][UNATTRIBUTED_KEY], 1);
});

test("folded teams sum into one column", () => {
  const teams = Array.from({ length: TEAM_SERIES_CAP + 2 }, (_, i) => ({ team: `t${i}`, daily: [day("2026-10-10", { turns: 1 })] }));
  const [row] = teamDailyRows(teams, { daily: [] }, "turns");
  assert.equal(row.__other, 2);
});

test("headline ratios are null without a denominator", () => {
  const empty = teamHeadline({ teams: [{ turns: 0, model_cost_micros: 0, failed_turns: 0 }], unattributed: { model_cost_micros: 0 } });
  assert.equal(empty.attributedShare, null);
  assert.equal(empty.failureRate, null);
  const h = teamHeadline({
    teams: [
      { turns: 8, failed_turns: 2, model_cost_micros: 3_000_000, daily_cost_alert_usd: 1, days_over_alert: 2 },
      { turns: 2, failed_turns: 0, model_cost_micros: 0, daily_cost_alert_usd: null, days_over_alert: null },
    ],
    unattributed: { model_cost_micros: 1_000_000 },
  });
  assert.equal(h.attributedShare, 0.75);
  assert.equal(h.failureRate, 0.2);
  assert.equal(h.teamsWithAlert, 1);
  assert.equal(h.teamsOverAlert, 1);
});

test("per member is null for an unknown or empty team", () => {
  assert.equal(perMember(10, null), null);
  assert.equal(perMember(10, 0), null);
  assert.equal(perMember(10, 4), 2.5);
});

test("alert series flags the days at or over the alert, none without one", () => {
  const team = { daily_cost_alert_usd: 2, daily: [day("2026-10-09", { model_cost_micros: 2_000_000 }), day("2026-10-10", { model_cost_micros: 1_999_999 })] };
  assert.deepEqual(alertSeries(team).map((p) => p.over), [true, false]);
  assert.deepEqual(alertSeries({ ...team, daily_cost_alert_usd: null }).map((p) => p.over), [false, false]);
});

test("decision rows keep ENFORCE blocks apart from LOG_ONLY would-denies", () => {
  const [row] = decisionRows([{ date: "2026-10-10", by_mode: { ENFORCE: { allow: 10, deny: 2 }, LOG_ONLY: { allow: 0, deny: 1 } } }]);
  assert.deepEqual(
    { a: row.enforce_allow, d: row.enforce_deny, l: row.log_only_deny },
    { a: 10, d: 2, l: 1 },
  );
});

test("denial matrix orders tools by total and finds the hottest cell", () => {
  const m = denialMatrix([
    { team: "fin", label: "재무", denied_tools: { "t___a": 1, "t___b": 4 } },
    { team: "hr", denied_tools: { "t___a": 2 } },
  ]);
  assert.deepEqual(m.tools, ["t___b", "t___a"]);
  assert.deepEqual(m.rows[0].cells, [4, 1]);
  assert.deepEqual(m.rows[1].cells, [0, 2]);
  assert.equal(m.max, 4);
  assert.deepEqual(m.toolTotals, [4, 3]);
});
