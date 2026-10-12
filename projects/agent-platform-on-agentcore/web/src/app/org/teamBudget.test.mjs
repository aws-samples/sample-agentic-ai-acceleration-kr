/**
 * The "오늘 / 일일 경고" cell: how today's spend reads against the team's alert.
 * Pure so the tiers can be pinned: no alert means no judgement, not "ok".
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { budgetTone, formatBudgetCell } from "./teamBudget.mjs";

test("no alert → muted and no ratio", () => {
  assert.deepEqual(budgetTone(1_000_000, null), { tone: "muted", ratio: null });
  assert.deepEqual(budgetTone(1_000_000, 0), { tone: "muted", ratio: null });
});

test("unknown spend → muted even with an alert", () => {
  assert.deepEqual(budgetTone(null, 5), { tone: "muted", ratio: null });
});

test("under 80% ok, 80% and above warn, at or over the alert error", () => {
  assert.equal(budgetTone(3_900_000, 5).tone, "ok");
  assert.equal(budgetTone(4_000_000, 5).tone, "warn");
  assert.equal(budgetTone(5_000_000, 5).tone, "error");
  assert.equal(budgetTone(0, 5).tone, "ok");
});

test("ratio is spend over alert", () => {
  assert.equal(budgetTone(2_500_000, 5).ratio, 0.5);
});

test("cell text: spend / alert, or — for a missing side", () => {
  assert.equal(formatBudgetCell(2_500_000, 5), "$2.5000 / $5.00");
  assert.equal(formatBudgetCell(0, 5), "$0.0000 / $5.00");
  assert.equal(formatBudgetCell(2_500_000, null), "$2.5000 / —");
  assert.equal(formatBudgetCell(null, 5), "— / $5.00");
});
