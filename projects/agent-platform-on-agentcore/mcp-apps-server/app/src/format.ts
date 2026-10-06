/**
 * Pure helpers for the telemetry view — kept free of DOM and Chart.js so
 * `node --test` can run them (Node 22.18+ strips types; erasable syntax only).
 */

export type View = "models" | "agents" | "tools";

export interface Series {
  id: string;
  label: string;
  kind: "model" | "runtime" | "harness" | "tool";
  totals: Record<string, number>;
  points: Record<string, number[]>;
}

export interface Telemetry {
  view: string;
  period: string;
  bucketSeconds: number;
  buckets: string[];
  series: Series[];
  generatedAt: string;
  note: string;
}

export interface Column {
  key: string;
  title: string;
  kind: "count" | "ms";
}

/** Table columns per view; the first column is what the server sorts by. */
export const COLUMNS: Record<View, Column[]> = {
  models: [
    { key: "Invocations", title: "호출", kind: "count" },
    { key: "InputTokenCount", title: "입력 토큰", kind: "count" },
    { key: "OutputTokenCount", title: "출력 토큰", kind: "count" },
    { key: "CacheReadInputTokenCount", title: "캐시 읽기", kind: "count" },
    { key: "CacheWriteInputTokenCount", title: "캐시 쓰기", kind: "count" },
    { key: "InvocationLatency", title: "지연", kind: "ms" },
    { key: "TimeToFirstToken", title: "TTFT", kind: "ms" },
  ],
  agents: [
    { key: "Invocations", title: "호출", kind: "count" },
    { key: "Latency", title: "지연", kind: "ms" },
    { key: "UserErrors", title: "사용자 오류", kind: "count" },
    { key: "SystemErrors", title: "시스템 오류", kind: "count" },
    { key: "Throttles", title: "스로틀", kind: "count" },
  ],
  tools: [
    { key: "Invocations", title: "호출", kind: "count" },
    { key: "Latency", title: "지연", kind: "ms" },
    { key: "Errors", title: "오류", kind: "count" },
  ],
};

/** Stacked token metrics for the models chart, in stack order. */
export const TOKEN_STACK = [
  { key: "InputTokenCount", title: "입력" },
  { key: "OutputTokenCount", title: "출력" },
  { key: "CacheReadInputTokenCount", title: "캐시 읽기" },
  { key: "CacheWriteInputTokenCount", title: "캐시 쓰기" },
];

export function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

export function formatMs(n: number): string {
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} s`;
  return `${Math.round(n)} ms`;
}

export function bucketLabels(t: Telemetry): string[] {
  return t.buckets.map((iso) => {
    // ISO "YYYY-MM-DDTHH:MM:SSZ" — slice instead of Date to stay in UTC and locale-free.
    const date = iso.slice(5, 10);
    const time = iso.slice(11, 16);
    return t.bucketSeconds >= 86400 ? date : time;
  });
}

/**
 * The `n` largest series by `metric` total, then one synthetic series that sums
 * the rest. Returned in descending order so stacks read top-down.
 */
export function topWithOthers(series: Series[], metric: string, n: number): Series[] {
  const sorted = [...series].sort((a, b) => (b.totals[metric] ?? 0) - (a.totals[metric] ?? 0));
  if (sorted.length <= n) return sorted;
  const head = sorted.slice(0, n);
  const tail = sorted.slice(n);
  const length = tail[0]?.points[metric]?.length ?? 0;
  const points: Record<string, number[]> = {};
  const totals: Record<string, number> = {};
  for (const key of Object.keys(tail[0].points)) {
    points[key] = Array.from({ length }, (_, i) =>
      tail.reduce((acc, s) => acc + (s.points[key]?.[i] ?? 0), 0),
    );
    totals[key] = tail.reduce((acc, s) => acc + (s.totals[key] ?? 0), 0);
  }
  head.push({ id: "__others__", label: `기타 (${tail.length})`, kind: tail[0].kind, totals, points });
  return head;
}
