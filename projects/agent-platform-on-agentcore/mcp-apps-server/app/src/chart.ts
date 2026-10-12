/**
 * Chart.js wiring for the telemetry view. One canvas, rebuilt on every data
 * change: stacked bars on the left axis, a line on the right axis.
 *
 * Only the pieces we use are registered — Chart.js 4 is tree-shakable and the
 * view ships as a single inlined file, so every unused controller is bytes in
 * `app.html`.
 */
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
  type ChartDataset,
} from "chart.js";

import {
  TOKEN_STACK,
  bucketLabels,
  formatMs,
  formatNumber,
  topWithOthers,
  type Series,
  type Telemetry,
  type View,
} from "./format.ts";

Chart.register(
  BarController,
  BarElement,
  CategoryScale,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
  Legend,
);

let chart: Chart | null = null;

/** Resolve a CSS colour to its computed rgb() string. */
function computedColor(value: string): string {
  const probe = document.createElement("span");
  probe.style.color = value;
  document.body.appendChild(probe);
  const rgb = getComputedStyle(probe).color;
  probe.remove();
  return rgb;
}

/** Palette derived from the host text colour so it reads on both themes. */
function palette(count: number): string[] {
  const fg = computedColor(getComputedStyle(document.documentElement).getPropertyValue("--fg").trim() || "CanvasText");
  const rgb = fg.match(/\d+/g)?.map(Number) ?? [128, 128, 128];
  const lightText = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114 > 140; // light text => dark theme
  const l = lightText ? 62 : 45;
  // Golden-angle hue steps keep neighbouring stacks apart even with 8+ series.
  return Array.from({ length: count }, (_, i) => `hsl(${(205 + i * 137.5) % 360} 62% ${l}%)`);
}

function textColor(): string {
  return computedColor(getComputedStyle(document.documentElement).getPropertyValue("--fg").trim() || "CanvasText");
}

function gridColor(): string {
  return computedColor(getComputedStyle(document.documentElement).getPropertyValue("--line").trim() || "rgba(128,128,128,.25)");
}


/**
 * Models: one stack of the four token kinds (for the selected model, or all
 * models summed) + invocations line. Agents/tools: one stack per item (top 8 +
 * others) + mean latency line.
 */
export function renderChart(canvas: HTMLCanvasElement, data: Telemetry, selected: Series | null): void {
  const view = data.view as View;
  const labels = bucketLabels(data);
  const fg = textColor();
  const bars: ChartDataset<"bar">[] = [];
  let line: ChartDataset<"line">;

  if (view === "models") {
    const pool = selected ? [selected] : data.series;
    const sum = (key: string) => labels.map((_, i) => pool.reduce((acc, s) => acc + (s.points[key]?.[i] ?? 0), 0));
    const colors = palette(TOKEN_STACK.length);
    TOKEN_STACK.forEach((m, i) =>
      bars.push({ label: m.title, data: sum(m.key), backgroundColor: colors[i], stack: "tokens", yAxisID: "y" }),
    );
    line = { type: "line", label: "호출", data: sum("Invocations"), yAxisID: "y1", borderColor: fg, backgroundColor: fg, borderDash: [4, 3], borderWidth: 1.5, pointRadius: 2, tension: 0.3 };
  } else {
    const pool = selected ? [selected] : topWithOthers(data.series, "Invocations", 8);
    const colors = palette(pool.length);
    pool.forEach((s, i) =>
      bars.push({ label: s.label, data: s.points.Invocations ?? [], backgroundColor: colors[i], stack: "calls", yAxisID: "y" }),
    );
    const latencyPool = selected ? [selected] : data.series;
    const mean = labels.map((_, i) => {
      const vals = latencyPool.map((s) => s.points.Latency?.[i] ?? 0).filter((v) => v > 0);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
    });
    line = { type: "line", label: "평균 지연", data: mean, yAxisID: "y1", borderColor: fg, backgroundColor: fg, borderDash: [4, 3], borderWidth: 1.5, pointRadius: 2, tension: 0.3 };
  }

  const rightIsMs = view !== "models";

  chart?.destroy();
  chart = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [...bars, line as unknown as ChartDataset<"bar">] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, color: fg } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const v = Number(item.parsed.y ?? 0);
              const isLine = item.dataset.yAxisID === "y1";
              return `${item.dataset.label}: ${isLine && rightIsMs ? formatMs(v) : formatNumber(v)}`;
            },
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: fg, maxRotation: 0, autoSkip: true } },
        y: {
          stacked: true,
          position: "left",
          grid: { color: gridColor() },
          ticks: { color: fg, callback: (v) => formatNumber(Number(v)) },
        },
        y1: {
          position: "right",
          grid: { drawOnChartArea: false },
          ticks: { color: fg, callback: (v) => (rightIsMs ? formatMs(Number(v)) : formatNumber(Number(v))) },
        },
      },
    },
  });
}

export function destroyChart(): void {
  chart?.destroy();
  chart = null;
}
