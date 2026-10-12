/**
 * How a team's spend today reads against its daily alert. Pure, so the tiers
 * are pinned by teamBudget.test.mjs and shared by the Organization table, the
 * team sheet and the Insights team panel.
 *
 * Display only: nothing here blocks a turn. The alert is a line the admin drew;
 * these helpers say which side of it today is on.
 */

/** Spend at or above this share of the alert reads as "근접" before it crosses. */
export const WARN_RATIO = 0.8;

/**
 * @param {number | null | undefined} todayMicros  today's model cost; null when the ledger was not read
 * @param {number | null | undefined} alertUsd     the team's daily alert; null/0 when none is set
 * @returns {{ tone: "ok" | "warn" | "error" | "muted", ratio: number | null }}
 *   `muted` with a null ratio whenever either side is missing — "no alert" is
 *   not "ok", it is "nothing to compare against".
 */
export function budgetTone(todayMicros, alertUsd) {
  if (todayMicros === null || todayMicros === undefined) return { tone: "muted", ratio: null };
  if (!alertUsd || alertUsd <= 0) return { tone: "muted", ratio: null };
  const ratio = todayMicros / (alertUsd * 1_000_000);
  if (ratio >= 1) return { tone: "error", ratio };
  if (ratio >= WARN_RATIO) return { tone: "warn", ratio };
  return { tone: "ok", ratio };
}

/** `$2.5000 / $5.00`; a missing side is `—`, never 0. */
export function formatBudgetCell(todayMicros, alertUsd) {
  const spend = todayMicros === null || todayMicros === undefined ? "—" : `$${(todayMicros / 1_000_000).toFixed(4)}`;
  const alert = alertUsd === null || alertUsd === undefined || alertUsd <= 0 ? "—" : `$${alertUsd.toFixed(2)}`;
  return `${spend} / ${alert}`;
}

const TONE_LABEL = { ok: "여유", warn: "근접", error: "초과" };

/** The badge word for a tone, or null when there is nothing to judge. */
export function budgetLabel(tone) {
  return TONE_LABEL[tone] ?? null;
}
