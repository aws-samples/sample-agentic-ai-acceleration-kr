/**
 * The one place a chart in this app gets its colours, chrome and number format.
 *
 * Two consumers with different jobs share it: `ChartRenderer` draws whatever
 * spec an agent produced, and the insights dashboard draws a fixed set of charts
 * it owns. They must not drift — a tooltip that is readable in dark mode on one
 * page and white-on-white on the other is the failure this module prevents.
 */

/**
 * Categorical series colours, read from the design tokens.
 *
 * Assigned in this fixed order and never cycled: colour follows the entity, so a
 * chart that drops a series must not repaint the survivors. The tokens carry
 * separate, individually validated light and dark steps, so referencing them by
 * name is also what makes the chart correct in both themes — see the comments in
 * globals.css for the validation figures.
 *
 * All eight sit at OKLCH chroma 0.105, the same loudness as a status chip. A chart
 * on this platform is not the loudest thing on its page, so reach for slot 1 plus
 * `MUTED_MARK` before reaching for a second hue: a second hue is a claim that the
 * two marks are *different entities*, and spending it on decoration is what makes
 * a page of four identical charts look like four different subjects.
 *
 * Past eight series the ninth is not a generated hue; `SERIES_CAP` folds the tail
 * into "기타" instead (`topNWithOther`).
 */
export const SERIES_COLORS = [
  "hsl(var(--chart-1))",
  "hsl(var(--chart-2))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
  "hsl(var(--chart-7))",
  "hsl(var(--chart-8))",
];
export const SERIES_CAP = SERIES_COLORS.length;

/**
 * Billed dollars, page-wide. Not one of SERIES_COLORS: cost is a nature of its own,
 * not a categorical series, and it must not read as the turns blue that `chart-1`
 * (the old default) gave it. A dedicated violet keeps every cost mark — the daily
 * column chart and the per-agent bars — on one hue that no metric uses.
 */
export const COST_COLOR = "hsl(var(--chart-cost))";

/**
 * One colour per *kind of quantity*, applied everywhere that quantity shows up on
 * the page — KPI sparkline, trend chart, composition bars, per-user lists — so a
 * reader learns a hue once and it means the same thing across every widget. Colour
 * carries meaning here, not decoration: same nature ⇒ same hue, different nature ⇒
 * different hue.
 *
 * The natures, and why their hue is shared:
 * - **turns / reach** (blue): a turn count. Also what the MCP-server, skill and
 *   knowledge-base composition bars measure (turns that reached the component) and
 *   what the per-user "turns" list is — all the same quantity, so all blue.
 * - **tool calls** (orange): a call count, a different quantity from turns, so a
 *   different hue — and the same orange in the KPI tile, the trend chart and the
 *   composition "툴 호출" list. Kept far from turns' blue on purpose; an earlier cut
 *   put it on indigo (chart-7) and it read as the same colour as turns.
 * - **tokens** (terracotta): token volume. `input_tokens` leads the token family;
 *   the per-user "tokens" list borrows it. `output_tokens` is the one place a second
 *   token hue (teal) is needed, because input and output share one stacked chart and
 *   must separate within it.
 * - **cache** (indigo read / pink write): the two cache tiers, distinct because they
 *   share one chart and are billed opposite ways.
 * - **failure endings** (rose failed / olive interrupted): the two ways a turn ends
 *   badly; distinct because their causes differ.
 *
 * The only reason two token/cache tiers get separate hues rather than one shared
 * nature-hue is that they are drawn *together* and must be told apart in the stack;
 * every single-series use across the page collapses to its nature's one colour.
 */
export const METRIC_COLOR: Record<string, number> = {
  turns: 0, // chart-1, blue — a turn count (also composition reach, per-user turns)
  input_tokens: 1, // chart-2, terracotta — the token-family hue
  output_tokens: 2, // chart-3, teal — second token tier, needs to separate in-stack
  tool_calls: 3, // chart-4, orange — a call count, kept far from turns' blue
  cache_write_tokens: 4, // chart-5, pink
  interrupted_turns: 5, // chart-6, olive
  cache_read_tokens: 6, // chart-7, indigo — paired with pink write, both distinct
  failed_turns: 7, // chart-8, rose
};

/**
 * The de-emphasis fill, for the "one series is the point, the rest are context"
 * case. A muted step rather than a ninth hue, so it can never be mistaken for an
 * entity of its own.
 */
export const MUTED_MARK = "hsl(var(--muted-foreground) / 0.35)";

/**
 * recharts' own tooltip is a white box with black text regardless of theme, so
 * every surface is restated in tokens. Without this the tooltip is unreadable in
 * dark mode.
 */
export const TOOLTIP_CONTENT = {
  backgroundColor: "hsl(var(--card))",
  border: "1px solid hsl(var(--border))",
  borderRadius: "var(--radius)",
  boxShadow: "0 4px 12px hsl(var(--foreground) / 0.08)",
  fontSize: "0.75rem",
} as const;
export const TOOLTIP_LABEL = { color: "hsl(var(--muted-foreground))" } as const;
export const TOOLTIP_ITEM = { color: "hsl(var(--card-foreground))" } as const;
/** Bar hover shade. recharts defaults to an opaque grey that fits neither theme. */
export const BAR_CURSOR = {
  fill: "hsl(var(--muted))",
  fillOpacity: 0.5,
} as const;
/** Line/area crosshair. */
export const AXIS_CURSOR = {
  stroke: "hsl(var(--muted-foreground))",
  strokeOpacity: 0.4,
  strokeDasharray: "3 3",
} as const;

export const AXIS = {
  stroke: "hsl(var(--muted-foreground))",
  fontSize: 11,
} as const;
export const GRID = { stroke: "hsl(var(--border))" } as const;
/** A 2px surface gap between adjacent fills, so bars read as separate marks. */
export const BAR_GAP = 2;
/**
 * Bars never fill their band: a column capped well under the slot width leaves
 * the leftover as air, which is what keeps a 30-day axis from reading as a solid
 * block of ink.
 */
export const BAR_MAX_SIZE = 18;
/** The area wash under a line — a tint, never a saturated block. */
export const AREA_OPACITY = 0.16;

/**
 * Axis ticks and headline figures, shortened.
 *
 * Applied to axes and headline numbers only — never to table cells, which keep
 * the full value so a reader can check the exact figure against the answer text.
 */
export function compactNumber(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return String(v ?? "");
  const abs = Math.abs(v);
  if (abs >= 1_0000_0000) return `${(v / 1_0000_0000).toFixed(1)}억`;
  if (abs >= 1_0000) return `${(v / 1_0000).toFixed(1)}만`;
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(2);
}
