/**
 * platform-telemetry MCP App (SEP-1865) view.
 *
 * `@modelcontextprotocol/ext-apps`'s `App` handles the host JSON-RPC over
 * postMessage (ui/initialize handshake, tool-input/tool-result, tools/call,
 * ui/update-model-context, size-changed). The first render comes from the
 * host's `tool-result` for `get_platform_telemetry`; every control afterwards
 * calls the app-only `query_platform_telemetry` — the model is not involved.
 *
 * Build: `npm run build` → `../app.html` (single file, SDK + Chart.js inlined).
 */
import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { renderChart } from "./chart.ts";
import { COLUMNS, formatMs, formatNumber, type Series, type Telemetry, type View } from "./format.ts";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const statusEl = $<HTMLDivElement>("status");
const tabsEl = $<HTMLDivElement>("tabs");
const periodsEl = $<HTMLDivElement>("periods");
const refreshBtn = $<HTMLButtonElement>("refresh");
const chartWrap = $<HTMLDivElement>("chartWrap");
const canvas = $<HTMLCanvasElement>("chart");
const table = $<HTMLTableElement>("table");
const pinBtn = $<HTMLButtonElement>("pin");

type Period = "1h" | "24h" | "7d";

const VIEW_TITLES: Record<View, string> = { models: "모델", agents: "에이전트", tools: "툴" };

const state = {
  view: "models" as View,
  period: "24h" as Period,
  data: null as Telemetry | null,
  selectedId: null as string | null,
  busy: false,
};

function setStatus(msg: string, isErr = false) {
  statusEl.textContent = msg;
  statusEl.classList.toggle("err", isErr);
}

/** 호스트가 준 테마·CSS 변수·글꼴을 문서에 반영한다. */
function applyHostContext(ctx: McpUiHostContext | undefined) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}

/** structuredContent first; text content as JSON fallback. Tool errors throw. */
function readResult(result: CallToolResult | undefined): Telemetry | null {
  if (!result) return null;
  if (result.isError) {
    const text = result.content.find((c) => c.type === "text");
    throw new Error(text && text.type === "text" ? text.text : "tool error");
  }
  const sc = result.structuredContent as Telemetry | undefined;
  if (sc && Array.isArray(sc.series)) return sc;
  const textBlock = result.content.find((c) => c.type === "text");
  if (!textBlock || textBlock.type !== "text") return null;
  return JSON.parse(textBlock.text) as Telemetry;
}

function selected(): Series | null {
  return state.data?.series.find((s) => s.id === state.selectedId) ?? null;
}

function setControlsEnabled(enabled: boolean) {
  for (const b of [...tabsEl.querySelectorAll("button"), ...periodsEl.querySelectorAll("button")]) {
    b.disabled = !enabled;
  }
  refreshBtn.disabled = !enabled;
  pinBtn.disabled = !enabled || !selected();
}

function syncSegments() {
  for (const b of tabsEl.querySelectorAll<HTMLButtonElement>("button")) {
    b.setAttribute("aria-pressed", String(b.dataset.view === state.view));
  }
  for (const b of periodsEl.querySelectorAll<HTMLButtonElement>("button")) {
    b.setAttribute("aria-pressed", String(b.dataset.period === state.period));
  }
}

function renderTable(data: Telemetry) {
  const view = data.view as View;
  const cols = COLUMNS[view];
  const thead = table.tHead!;
  const tbody = table.tBodies[0];
  thead.replaceChildren();
  tbody.replaceChildren();

  const hr = document.createElement("tr");
  const nameTh = document.createElement("th");
  nameTh.textContent = VIEW_TITLES[view];
  hr.appendChild(nameTh);
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.title;
    hr.appendChild(th);
  }
  thead.appendChild(hr);

  for (const s of data.series) {
    const tr = document.createElement("tr");
    tr.dataset.id = s.id;
    tr.title = s.id;
    tr.setAttribute("aria-selected", String(s.id === state.selectedId));
    const nameTd = document.createElement("td");
    nameTd.textContent = s.label;
    // Disambiguate: harnesses among runtimes, and the gateway target for tools
    // (two targets can expose the same tool name, e.g. WebSearch).
    const badge = s.kind === "harness" ? "harness" : s.kind === "tool" && s.id.includes("___") ? s.id.split("___")[0] : null;
    if (badge) {
      const k = document.createElement("span");
      k.className = "kind";
      k.textContent = badge;
      nameTd.appendChild(k);
    }
    tr.appendChild(nameTd);
    for (const c of cols) {
      const td = document.createElement("td");
      const v = s.totals[c.key] ?? 0;
      td.textContent = c.kind === "ms" ? formatMs(v) : formatNumber(v);
      tr.appendChild(td);
    }
    tr.onclick = () => {
      state.selectedId = state.selectedId === s.id ? null : s.id;
      render();
    };
    tbody.appendChild(tr);
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  syncSegments();
  chartWrap.classList.toggle("empty", data.series.length === 0);
  renderChart(canvas, data, selected());
  renderTable(data);
  pinBtn.disabled = state.busy || !selected();
  const at = data.generatedAt.slice(11, 19);
  setStatus(`${data.series.length}개 항목 · ${data.period} · ${at} UTC 기준`);
}

const app = new App(
  { name: "platform-telemetry-app", version: "2.0.0" },
  { availableDisplayModes: ["inline"] },
  // autoResize(기본 true): body/documentElement의 ResizeObserver로
  // ui/notifications/size-changed를 자동 보고한다 (규격 SHOULD).
);

/** Re-query through the app-only tool; the model is not in this path. */
async function query(view: View, period: Period) {
  state.busy = true;
  setControlsEnabled(false);
  setStatus("조회 중…");
  try {
    const result = await app.callServerTool({
      name: "query_platform_telemetry",
      arguments: { view, period },
    });
    const data = readResult(result);
    if (!data) throw new Error("empty result");
    if (view !== state.view) state.selectedId = null;
    state.view = view;
    state.period = period;
    state.data = data;
    render();
  } catch (e) {
    setStatus(`조회 실패: ${(e as Error).message}`, true);
  } finally {
    state.busy = false;
    setControlsEnabled(true);
  }
}

tabsEl.onclick = (ev) => {
  const b = (ev.target as HTMLElement).closest<HTMLButtonElement>("button[data-view]");
  if (!b || b.disabled) return;
  void query(b.dataset.view as View, state.period);
};
periodsEl.onclick = (ev) => {
  const b = (ev.target as HTMLElement).closest<HTMLButtonElement>("button[data-period]");
  if (!b || b.disabled) return;
  void query(state.view, b.dataset.period as Period);
};
refreshBtn.onclick = () => void query(state.view, state.period);

pinBtn.onclick = async () => {
  const s = selected();
  const data = state.data;
  if (!s || !data) return;
  pinBtn.disabled = true;
  try {
    // 다음 턴에 모델이 이 항목의 수치를 정확히 참조할 수 있게 컨텍스트로 넘긴다.
    await app.updateModelContext({
      structuredContent: {
        telemetry: {
          view: data.view,
          period: data.period,
          generatedAt: data.generatedAt,
          item: { id: s.id, label: s.label, kind: s.kind, totals: s.totals },
        },
      },
    });
    setStatus(`"${s.label}" 수치를 모델 컨텍스트에 넣었습니다 — 다음 턴에 모델이 참고합니다.`);
  } catch (e) {
    setStatus(`컨텍스트 갱신 실패: ${(e as Error).message}`, true);
  } finally {
    pinBtn.disabled = false;
  }
};

// The model's arguments: match the controls to what it asked for before data lands.
app.ontoolinput = (params) => {
  const args = (params.arguments ?? {}) as { view?: View; period?: Period };
  if (args.view && args.view in COLUMNS) state.view = args.view;
  if (args.period === "1h" || args.period === "24h" || args.period === "7d") state.period = args.period;
  syncSegments();
};

app.ontoolresult = (result) => {
  try {
    const data = readResult(result);
    if (!data) throw new Error("empty result");
    state.view = data.view as View;
    state.period = data.period as Period;
    state.data = data;
    render();
  } catch (e) {
    setStatus(`결과 오류: ${(e as Error).message}`, true);
  }
  setControlsEnabled(true);
};

app.ontoolcancelled = (params) => {
  setStatus(
    `툴 호출이 취소되었습니다${params.reason ? ` (${params.reason})` : ""}. 새로 고침으로 다시 조회할 수 있습니다.`,
  );
  refreshBtn.disabled = false;
};

app.onhostcontextchanged = (ctx) => {
  applyHostContext(ctx);
  render(); // palette follows the theme
};

app.onteardown = async () => ({});

// 규격 순서: ui/initialize → (응답) → ui/notifications/initialized. SDK가 처리한다.
app
  .connect()
  .then(() => {
    applyHostContext(app.getHostContext());
    setStatus("호스트와 연결됨 · 결과를 기다리는 중…");
  })
  .catch((e: Error) => setStatus(`호스트 연결 실패: ${e.message}`, true));
