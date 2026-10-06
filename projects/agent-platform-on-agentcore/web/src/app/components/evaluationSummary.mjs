/**
 * Evaluation score and session summary presentation.
 *
 * **Why null scores are not zero:** An evaluator that failed on every session
 * (e.g., the model refused, or the thread had no AI turn) has no score data to
 * report. That state — "never computed" — is distinct from a measured 0.02 on
 * an actual dimension like Correctness, and rendering both as "0.00" would be
 * statistically misleading.
 *
 * **Why scores are never averaged:** Each evaluator runs on a thread or session,
 * and different evaluators measure different things. Averaging a Correctness
 * score (TRACE level) with a ToolSelectionAccuracy score (TOOL_CALL level) has
 * no semantic meaning — they are on incompatible scales. Display each evaluator's
 * result independently.
 */

const LEVEL_LABELS = {
  TRACE: "턴 단위",
  SESSION: "세션 단위",
  TOOL_CALL: "툴 호출 단위",
};

const LEVEL_ORDER = ["TRACE", "SESSION", "TOOL_CALL"];

/**
 * Format a score to two decimal places, or "모름" if null.
 */
export function scoreLabel(score) {
  if (score === null || score === undefined) return "모름";
  return score.toFixed(2);
}

/**
 * Map score to tone: "good" (≥0.8), "warn" (≥0.5), "bad", or "unknown".
 */
export function scoreTone(score) {
  if (score === null || score === undefined) return "unknown";
  if (score >= 0.8) return "good";
  if (score >= 0.5) return "warn";
  return "bad";
}

/**
 * Group evaluators by level in a fixed order, with label.
 */
export function groupByLevel(evaluators) {
  const byLevel = {};
  for (const evaluator of evaluators) {
    const level = evaluator.level;
    if (!byLevel[level]) {
      byLevel[level] = [];
    }
    byLevel[level].push(evaluator);
  }

  const groups = [];
  for (const level of LEVEL_ORDER) {
    if (byLevel[level]) {
      groups.push({
        level,
        label: LEVEL_LABELS[level],
        evaluators: byLevel[level],
      });
    }
  }

  // Add any unknown levels that are not in the fixed order
  for (const level of Object.keys(byLevel)) {
    if (!LEVEL_ORDER.includes(level)) {
      groups.push({
        level,
        label: level,
        evaluators: byLevel[level],
      });
    }
  }

  return groups;
}

/**
 * Format session summary string from session counts.
 */
export function sessionSummary(sessions) {
  const { total, completed, failed, in_progress } = sessions;
  const notCompleted = total - completed;

  if (completed === total) {
    return `${total}개 세션 모두 완료`;
  }

  if (in_progress > 0 && completed === 0 && failed === 0) {
    return `${total}개 세션 중 ${in_progress}개 진행 중`;
  }

  if (failed > 0) {
    return `${total}개 세션 중 ${completed}개 완료 · ${failed}개 실패`;
  }

  return `${total}개 세션 중 ${completed}개 완료`;
}

/**
 * Check if a status indicates the evaluation is still running.
 */
export function isPending(status) {
  return status === "PENDING" || status === "IN_PROGRESS" || status === "STOPPING";
}
