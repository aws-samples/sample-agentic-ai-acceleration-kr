/**
 * Dashboard layout model: WIDGET_IDS, reconciliation, and mutations.
 *
 * Pure functions, no imports, no mutation. Every function returns a new array.
 * The reconciliation logic mirrors server/services/layout_service.py::reconcile
 * case for case. Do not change one without changing the other.
 */

export const LAYOUT_VERSION = 1;
export const WIDGET_IDS = [
  "kpi",
  "leaderboard",
  "cost_composition",
  "model_mix",
  "billed",
  "trend",
  "reuse",
  "users",
  "guardrail",
  "teams",
];
const SPANS = ["half", "full"];

export const DEFAULT_LAYOUT = {
  version: LAYOUT_VERSION,
  widgets: WIDGET_IDS.map((id) => ({
    id,
    span: "full",
    visible: true,
  })),
};

/**
 * A stored layout reconciled against the registry. Never throws.
 *
 * The layout is a hint and the registry (WIDGET_IDS) is the authority.
 * Three reconciliation rules protect against failures that would blank the dashboard:
 *
 * - An id the registry no longer has is dropped, so a widget rename does not
 *   corrupt a saved layout.
 * - A registered id the layout lacks is appended visible, so a new widget
 *   shipped after a layout was saved is not permanently invisible to everyone.
 * - Anything unrecognised — wrong version, bad span, malformed shape — falls
 *   back to the default, because there is no version of this feature where a
 *   broken preference should be more important than seeing the page.
 */
export function reconcileLayout(stored) {
  if (typeof stored !== "object" || stored === null) {
    return { version: LAYOUT_VERSION, widgets: [...DEFAULT_LAYOUT.widgets] };
  }
  if (stored.version !== LAYOUT_VERSION) {
    return { version: LAYOUT_VERSION, widgets: [...DEFAULT_LAYOUT.widgets] };
  }

  const raw = stored.widgets;
  if (!Array.isArray(raw)) {
    return { version: LAYOUT_VERSION, widgets: [...DEFAULT_LAYOUT.widgets] };
  }

  const widgets = [];
  const seen = new Set();

  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const widgetId = entry.id;
    if (!WIDGET_IDS.includes(widgetId) || seen.has(widgetId)) {
      continue;
    }
    seen.add(widgetId);
    const span = SPANS.includes(entry.span) ? entry.span : "full";
    widgets.push({
      id: widgetId,
      span,
      visible: Boolean(entry.visible ?? true),
    });
  }

  if (widgets.length === 0) {
    return { version: LAYOUT_VERSION, widgets: [...DEFAULT_LAYOUT.widgets] };
  }

  // Append any registered ids that are not in the stored layout.
  for (const widgetId of WIDGET_IDS) {
    if (!seen.has(widgetId)) {
      widgets.push({
        id: widgetId,
        span: "full",
        visible: true,
      });
    }
  }

  return { version: LAYOUT_VERSION, widgets };
}

/**
 * Move a widget to a new position without mutating the input.
 *
 * - If source or target id not found, returns input unchanged.
 * - If source === target, returns input unchanged.
 */
export function moveWidget(widgets, fromId, toId) {
  if (fromId === toId) {
    return widgets;
  }

  const fromIndex = widgets.findIndex((w) => w.id === fromId);
  const toIndex = widgets.findIndex((w) => w.id === toId);

  if (fromIndex === -1 || toIndex === -1) {
    return widgets;
  }

  const newWidgets = widgets.map((w) => ({ ...w }));
  const [removed] = newWidgets.splice(fromIndex, 1);
  newWidgets.splice(toIndex, 0, removed);

  return newWidgets;
}

/**
 * Toggle visibility of a widget without reordering.
 */
export function toggleWidget(widgets, id) {
  return widgets.map((w) =>
    w.id === id ? { ...w, visible: !w.visible } : { ...w },
  );
}

/**
 * Set the span of a widget, accepting only "half" and "full".
 *
 * Invalid spans default to "full".
 */
export function setSpan(widgets, id, span) {
  const validSpan = SPANS.includes(span) ? span : "full";
  return widgets.map((w) =>
    w.id === id ? { ...w, span: validSpan } : { ...w },
  );
}

/**
 * Return only visible widgets in their current order.
 */
export function visibleWidgets(widgets) {
  return widgets.filter((w) => w.visible);
}

/**
 * Map a span value to a Tailwind grid column class.
 *
 * - "full" → "col-span-2" (two columns on a 2-column grid)
 * - "half" → "col-span-1" (one column)
 * - anything else → "col-span-2" (safe fallback)
 */
export function spanClass(span) {
  return span === "half" ? "col-span-1" : "col-span-2";
}
