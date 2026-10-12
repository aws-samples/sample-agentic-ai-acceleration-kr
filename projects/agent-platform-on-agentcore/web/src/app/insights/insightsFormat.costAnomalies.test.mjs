import assert from "node:assert";
import test from "node:test";
import { costAnomalies } from "./insightsFormat.mjs";

test("flags a day whose cost exceeds trailing median * k", () => {
  const daily = [
    { date: "2026-08-01", cost: 1 }, { date: "2026-08-02", cost: 1 },
    { date: "2026-08-03", cost: 1 }, { date: "2026-08-04", cost: 10 },
  ];
  const out = costAnomalies(daily, { k: 2, minSamples: 3 });
  assert.deepEqual(out.map((a) => a.date), ["2026-08-04"]);
});

test("excludes the partial day and needs minSamples", () => {
  const daily = [
    { date: "2026-08-01", cost: 1 }, { date: "2026-08-02", cost: 99 },
  ];
  assert.deepEqual(costAnomalies(daily, { minSamples: 3 }), [], "too few samples");
  const daily2 = [
    { date: "2026-08-01", cost: 1 }, { date: "2026-08-02", cost: 1 },
    { date: "2026-08-03", cost: 1 }, { date: "2026-08-04", cost: 99 },
  ];
  assert.deepEqual(
    costAnomalies(daily2, { partialDay: "2026-08-04", minSamples: 3 }), [],
    "the spike is the partial day, so it is not judged",
  );
});

test("a flat series has no anomalies", () => {
  const daily = Array.from({ length: 7 }, (_, i) => ({
    date: `2026-08-0${i + 1}`, cost: 5,
  }));
  assert.deepEqual(costAnomalies(daily), []);
});
