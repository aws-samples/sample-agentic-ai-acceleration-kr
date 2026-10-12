import test from "node:test";
import assert from "node:assert/strict";

import {
  groupByLevel,
  isPending,
  scoreLabel,
  scoreTone,
  sessionSummary,
} from "./evaluationSummary.mjs";

test("a score renders with two decimals, and absence is not zero", () => {
  assert.equal(scoreLabel(0.02), "0.02");
  assert.equal(scoreLabel(1), "1.00");
  assert.equal(scoreLabel(0), "0.00");
  // An evaluator that failed on every session has no score. That is not 0.00 —
  // a real 0.02 was measured on Conciseness and means something quite different.
  assert.equal(scoreLabel(null), "모름");
});

test("tone thresholds", () => {
  assert.equal(scoreTone(0.9), "good");
  assert.equal(scoreTone(0.6), "warn");
  assert.equal(scoreTone(0.2), "bad");
  assert.equal(scoreTone(null), "unknown");
});

test("evaluators group by level in a fixed order", () => {
  const groups = groupByLevel([
    { evaluator_id: "Builtin.ToolSelectionAccuracy", level: "TOOL_CALL", builtin: true },
    { evaluator_id: "Builtin.Correctness", level: "TRACE", builtin: true },
    { evaluator_id: "Builtin.GoalSuccessRate", level: "SESSION", builtin: true },
  ]);
  assert.deepEqual(groups.map((g) => g.level), ["TRACE", "SESSION", "TOOL_CALL"]);
  assert.equal(groups[0].label, "턴 단위");
  assert.equal(groups[1].label, "세션 단위");
  assert.equal(groups[2].label, "툴 호출 단위");
});

test("an unknown level still appears rather than being dropped", () => {
  const groups = groupByLevel([{ evaluator_id: "x", level: "SOMETHING_NEW" }]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].label, "SOMETHING_NEW");
});

test("partial failure is part of the reading, not an error", () => {
  assert.equal(
    sessionSummary({ total: 28, completed: 27, failed: 1, in_progress: 0, ignored: 0 }),
    "28개 세션 중 27개 완료 · 1개 실패",
  );
  assert.equal(
    sessionSummary({ total: 3, completed: 3, failed: 0, in_progress: 0, ignored: 0 }),
    "3개 세션 모두 완료",
  );
  assert.equal(
    sessionSummary({ total: 3, completed: 0, failed: 0, in_progress: 3, ignored: 0 }),
    "3개 세션 중 3개 진행 중",
  );
});

test("isPending covers every non-terminal AgentCore status", () => {
  for (const s of ["PENDING", "IN_PROGRESS", "STOPPING"]) {
    assert.equal(isPending(s), true, s);
  }
  for (const s of ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"]) {
    assert.equal(isPending(s), false, s);
  }
});
