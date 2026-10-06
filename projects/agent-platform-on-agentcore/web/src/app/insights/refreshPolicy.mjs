/**
 * When the insights page should re-read itself.
 *
 * **What the poll may touch is the whole design.** It calls `/summary`, which
 * reads the usage table and a Cost Explorer figure cached for six hours. It does
 * not call `/telemetry`: `GetMetricData` bills per metric requested, and the
 * sweep behind that route was measured at 279 metrics — $0.0028 a poll, $4.02 a
 * day per tab left open, once per tab. An earlier version of this comment
 * claimed the CloudWatch sweep was "cached for 300s"; only the ListMetrics
 * discovery was, so the metered part of it was charged on every single poll.
 *
 * Live streaming was considered and rejected on measurement, not taste: the
 * usage table is written at flush time rather than continuously, so a stream
 * would push the same number repeatedly.
 *
 * So: a one-minute poll of the free tier, suspended whenever nobody is looking,
 * with a visible stamp so a stale figure never passes for a fresh one — and a
 * button for the tier that costs something.
 *
 * Pure functions in a .mjs module so they are testable with `node --test`,
 * with no browser and no React.
 */

export const POLL_INTERVAL_MS = 60000;

export function shouldPoll({ visibility, lastAt, now, intervalMs } = {}) {
  if (visibility === "hidden") return false;
  if (lastAt === null || lastAt === undefined) return true;
  return now - lastAt >= (intervalMs ?? POLL_INTERVAL_MS);
}

export function stampLabel(lastAt, now) {
  if (lastAt === null || lastAt === undefined) return "아직 읽지 않음";
  const seconds = Math.floor((now - lastAt) / 1000);
  if (seconds < 5) return "방금";
  if (seconds < 60) return `${seconds}초 전`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  return `${Math.floor(minutes / 60)}시간 전`;
}
