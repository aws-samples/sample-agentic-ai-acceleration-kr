// Run: node --test src/format.test.ts  (Node 22.18+ strips types natively)
import assert from "node:assert/strict";
import { test } from "node:test";

import { bucketLabels, formatMs, formatNumber, topWithOthers, type Series, type Telemetry } from "./format.ts";

const series = (id: string, inv: number[], lat: number[] = inv.map(() => 0)): Series => ({
  id,
  label: id,
  kind: "runtime",
  totals: { Invocations: inv.reduce((a, b) => a + b, 0), Latency: 0 },
  points: { Invocations: inv, Latency: lat },
});

test("numbers are compact and locale-neutral", () => {
  assert.equal(formatNumber(0), "0");
  assert.equal(formatNumber(941424), "941.4K");
  assert.equal(formatNumber(2_300_000), "2.3M");
  assert.equal(formatNumber(12.4), "12");
});

test("latency in ms becomes seconds above a thousand", () => {
  assert.equal(formatMs(0), "0 ms");
  assert.equal(formatMs(834), "834 ms");
  assert.equal(formatMs(127332), "127.3 s");
});

test("bucket labels depend on the bucket size", () => {
  const base = { view: "models", period: "1h", series: [], generatedAt: "", note: "" };
  const hourly: Telemetry = { ...base, bucketSeconds: 3600, buckets: ["2026-09-23T00:00:00Z", "2026-09-23T13:00:00Z"] };
  assert.deepEqual(bucketLabels(hourly), ["00:00", "13:00"]);
  const daily: Telemetry = { ...base, bucketSeconds: 86400, buckets: ["2026-09-17T00:00:00Z", "2026-09-23T00:00:00Z"] };
  assert.deepEqual(bucketLabels(daily), ["09-17", "09-23"]);
  const fine: Telemetry = { ...base, bucketSeconds: 300, buckets: ["2026-09-23T11:35:00Z"] };
  assert.deepEqual(bucketLabels(fine), ["11:35"]);
});

test("top N keeps the biggest and folds the rest into one summed series", () => {
  const all = [series("a", [1, 0]), series("b", [5, 5]), series("c", [2, 0]), series("d", [0, 2])];
  const out = topWithOthers(all, "Invocations", 2);
  assert.deepEqual(out.map((s) => s.label), ["b", "c", "기타 (2)"]);
  assert.deepEqual(out[2].points.Invocations, [1, 2]);
  assert.equal(out[2].id, "__others__");
});

test("top N with few series returns them unchanged and without an others row", () => {
  const all = [series("a", [1]), series("b", [5])];
  assert.deepEqual(topWithOthers(all, "Invocations", 8).map((s) => s.id), ["b", "a"]);
});
