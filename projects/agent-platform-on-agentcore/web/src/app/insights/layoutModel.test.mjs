import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_LAYOUT,
  WIDGET_IDS,
  moveWidget,
  reconcileLayout,
  setSpan,
  spanClass,
  toggleWidget,
  visibleWidgets,
} from "./layoutModel.mjs";

const layout = () => DEFAULT_LAYOUT.widgets.map((w) => ({ ...w }));

test("the client's widget list matches the server's", () => {
  // The server validates layouts against its own WIDGET_IDS. If the two drift,
  // a widget the client offers is silently dropped on save and the user's change
  // appears to have been ignored. Kept as a literal rather than derived, because
  // the point is to fail when only one side is edited — which is what adding
  // `users` and `guardrail` to serve the per-user and governance panels did.
  assert.deepEqual(WIDGET_IDS, [
    "kpi",
    "leaderboard",
    "cost_composition",
    "model_mix",
    "billed",
    "trend",
    "reuse",
    "users",
    "guardrail",
  ]);
});

test("moveWidget reorders without mutating its input", () => {
  const before = layout();
  const after = moveWidget(before, "trend", "kpi");

  assert.equal(after[0].id, "trend");
  assert.equal(before[0].id, "kpi", "input must not be mutated");
});

test("moving a widget onto itself changes nothing", () => {
  const before = layout();
  assert.deepEqual(moveWidget(before, "kpi", "kpi"), before);
});

test("moving an unknown id is a no-op rather than a crash", () => {
  const before = layout();
  assert.deepEqual(moveWidget(before, "ghost", "kpi"), before);
  assert.deepEqual(moveWidget(before, "kpi", "ghost"), before);
});

test("toggleWidget flips visibility without reordering", () => {
  const after = toggleWidget(layout(), "billed");
  assert.equal(after.find((w) => w.id === "billed").visible, false);
  assert.deepEqual(after.map((w) => w.id), WIDGET_IDS);
});

test("setSpan accepts only half and full", () => {
  assert.equal(setSpan(layout(), "kpi", "half").find((w) => w.id === "kpi").span, "half");
  assert.equal(setSpan(layout(), "kpi", "quarter").find((w) => w.id === "kpi").span, "full");
});

test("visibleWidgets keeps order and drops the hidden", () => {
  const after = toggleWidget(layout(), "leaderboard");
  assert.deepEqual(
    visibleWidgets(after).map((w) => w.id),
    WIDGET_IDS.filter((id) => id !== "leaderboard"),
  );
});

test("spanClass maps to grid columns, not to pixel widths", () => {
  assert.match(spanClass("full"), /col-span-2/);
  assert.match(spanClass("half"), /col-span-1/);
  assert.match(spanClass("nonsense"), /col-span-2/);
});

test("reconcileLayout mirrors the server: unknown ids dropped, new ids appended", () => {
  const result = reconcileLayout({
    version: 1,
    widgets: [
      { id: "gone", span: "full", visible: true },
      { id: "trend", span: "half", visible: false },
    ],
  });

  assert.equal(result.widgets[0].id, "trend");
  assert.equal(result.widgets[0].visible, false);
  assert.deepEqual(
    new Set(result.widgets.map((w) => w.id)),
    new Set(WIDGET_IDS),
  );
});

test("reconcileLayout survives every shape of garbage", () => {
  for (const junk of [null, undefined, {}, { widgets: 3 }, { version: 99, widgets: [] }]) {
    assert.deepEqual(reconcileLayout(junk).widgets, DEFAULT_LAYOUT.widgets);
  }
});

test("a layout saved before the cost widgets existed gets them appended", () => {
  const result = reconcileLayout({
    version: 1,
    widgets: [
      { id: "kpi", span: "full", visible: true },
      { id: "billed", span: "full", visible: true },
    ],
  });
  const ids = result.widgets.map((w) => w.id);
  assert.deepEqual(ids.slice(0, 2), ["kpi", "billed"]);
  assert.ok(ids.includes("cost_composition") && ids.includes("model_mix"));
  assert.ok(result.widgets.find((w) => w.id === "model_mix").visible);
});
