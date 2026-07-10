// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// BI Insight 분석 결과(서술 본문 + recharts 차트 + 데이터 표 + 실행 SQL)를 화면에
// 보이는 그대로 모아 **자체완결 HTML 리포트**로 만들고, 새 창에서 인쇄(→ "PDF로
// 저장")를 띄운다. 차트는 화면에 이미 렌더된 recharts SVG 를 그대로 떼어내므로
// **벡터(고품질) 그대로** 보존된다(matplotlib PNG 재집계 경로와 달리 무손실·즉시).
//
// 핵심 기법:
//  - SVG 색은 `hsl(var(--chart-N))` 등 CSS 변수 기반이라 독립 문서로 옮기면 깨진다.
//    캡처 시 getComputedStyle 로 **구체 색을 인라인**해 테마 의존성을 끊는다.
//  - 캡처 직전 `.dark` 클래스를 잠시 제거 → 인쇄에 적합한 라이트 톤으로 고정(차트
//    팔레트 chart-1~5 는 라이트/다크 동일하므로 데이터 마크 색은 불변, 축/격자/
//    텍스트만 인쇄용 진한 잉크로 바뀐다). CSS 변수 기반이라 React 재렌더 불필요.

import type { ChatMessage } from './types';

// recharts SVG 로 렌더되는 차트 종류(table/kpi/image 는 SVG 아님 — 데이터 표로 대체).
const SVG_CHART_KINDS = new Set(['bar', 'line', 'area', 'pie']);

// 차트 SVG 에서 구체값으로 고정할 표현 속성(detach 후에도 벡터·색 유지).
const SVG_PAINT_PROPS = [
  'fill',
  'fill-opacity',
  'stroke',
  'stroke-width',
  'stroke-opacity',
  'stroke-dasharray',
  'opacity',
  'color',
  'font-family',
  'font-size',
  'font-weight',
  'text-anchor',
] as const;

export function escapeHtml(s: unknown): string {
  return String(s ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  );
}

// data(객체 배열) → 정적 HTML 표. 숫자 컬럼은 우측정렬(.num). ChartRenderer 의
// DataTable 과 같은 데이터원을 쓰되, 리포트용으로 클래스 의존 없는 순수 표로 굽는다.
export function tableHtml(data: Record<string, unknown>[] | undefined): string {
  if (!data || data.length === 0) return '';
  const cols = Object.keys(data[0]);
  const numericCols = new Set(
    cols.filter((c) => {
      const vals = data.map((r) => r[c]).filter((v) => v != null);
      return vals.length > 0 && vals.every((v) => typeof v === 'number');
    }),
  );
  const head = cols.map((c) => `<th class="${numericCols.has(c) ? 'num' : ''}">${escapeHtml(c)}</th>`).join('');
  const body = data
    .map((r) => {
      const tds = cols
        .map((c) => {
          const v = r[c];
          const cell = typeof v === 'object' && v !== null ? JSON.stringify(v) : v;
          return `<td class="${numericCols.has(c) ? 'num' : ''}">${escapeHtml(cell)}</td>`;
        })
        .join('');
      return `<tr>${tds}</tr>`;
    })
    .join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// 라이브 SVG → 인라인 색 + 반응형 스케일을 적용한 자체완결 SVG 문자열.
function captureChartSvg(live: SVGSVGElement): string {
  const clone = live.cloneNode(true) as SVGSVGElement;
  const liveAll = [live, ...Array.from(live.querySelectorAll('*'))];
  const cloneAll = [clone, ...Array.from(clone.querySelectorAll('*'))];
  for (let i = 0; i < liveAll.length; i++) {
    const target = cloneAll[i] as SVGElement | undefined;
    if (!target) continue;
    const cs = getComputedStyle(liveAll[i]);
    let style = '';
    for (const p of SVG_PAINT_PROPS) {
      const v = cs.getPropertyValue(p);
      if (v) style += `${p}:${v};`;
    }
    target.setAttribute('style', style);
  }

  // viewBox 를 부여하고 폭 100% 로 풀어 인쇄 컬럼 폭에 맞춰 벡터 스케일(무손실).
  const w = parseFloat(live.getAttribute('width') || String(live.clientWidth) || '0');
  const h = parseFloat(live.getAttribute('height') || String(live.clientHeight) || '0');
  if (w && h && !clone.getAttribute('viewBox')) {
    clone.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }
  clone.setAttribute('width', '100%');
  clone.removeAttribute('height');
  clone.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  clone.style.height = 'auto';
  if (w) clone.style.maxWidth = `${w}px`;
  return clone.outerHTML;
}

// recharts 범례(HTML, SVG 밖·절대배치)를 정적 블록으로 캡처(텍스트색 인라인).
function captureLegend(legend: Element): string {
  const clone = legend.cloneNode(true) as HTMLElement;
  // recharts 는 범례를 position:absolute(left/top/transform)로 띄운다 — 정적화.
  Object.assign(clone.style, {
    position: 'static',
    width: '100%',
    height: 'auto',
    left: '',
    top: '',
    right: '',
    bottom: '',
    transform: '',
    textAlign: 'center',
  });
  // 텍스트색은 테마 의존(다크면 라이트색) — 캡처 시점(강제 라이트)의 computed 를 박는다.
  const liveTexts = Array.from(legend.querySelectorAll('.recharts-legend-item-text'));
  const cloneTexts = Array.from(clone.querySelectorAll<HTMLElement>('.recharts-legend-item-text'));
  liveTexts.forEach((el, i) => {
    if (cloneTexts[i]) cloneTexts[i].style.color = getComputedStyle(el).color;
  });
  return `<div class="legend">${clone.outerHTML}</div>`;
}

// 실행된 SQL 을 toolCalls/verifications 에서 수집(부록용). 중복 제거.
function collectSql(message: ChatMessage): string[] {
  const out = new Set<string>();
  for (const tc of message.toolCalls ?? []) {
    const r = tc.result as Record<string, unknown> | undefined;
    for (const key of ['sql', 'chosen_sql']) {
      const v = r?.[key];
      if (typeof v === 'string' && v.trim()) out.add(v.trim());
    }
  }
  for (const v of message.verifications ?? []) {
    if (v.chosen_sql && v.chosen_sql.trim()) out.add(v.chosen_sql.trim());
  }
  return [...out];
}

// 차트 섹션(SVG + 범례 + 데이터 표)을 message.charts 순서대로 조립.
// 캡처는 호출자가 .dark 강제해제 컨텍스트에서 부른다(라이트 톤 고정).
function buildChartsHtml(root: HTMLElement, message: ChatMessage): string {
  const liveSvgs = Array.from(root.querySelectorAll<SVGSVGElement>('svg.recharts-surface'));
  let svgIdx = 0;
  let html = '';
  for (const spec of message.charts ?? []) {
    const title = spec.title ? `<h3 class="chart-title">${escapeHtml(spec.title)}</h3>` : '';
    if (SVG_CHART_KINDS.has(spec.kind)) {
      const live = liveSvgs[svgIdx++];
      let chart = '';
      if (live) {
        const wrapper = live.closest('.recharts-wrapper');
        const legendEl = wrapper?.querySelector('.recharts-legend-wrapper');
        const legend = legendEl ? captureLegend(legendEl) : '';
        chart = `<div class="chart">${captureChartSvg(live)}${legend}</div>`;
      }
      html += `<section class="chart-block">${title}${chart}${tableHtml(spec.data)}</section>`;
    } else {
      // table / kpi / image → 데이터 표로 표현.
      html += `<section class="chart-block">${title}${tableHtml(spec.data)}</section>`;
    }
  }
  return html;
}

const REPORT_CSS = `
:root{
  --ink:#0f1115; --muted:#5e6673; --line:#e6e9ef; --brand:#0d9488; --bg:#ffffff;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);
  font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',system-ui,sans-serif;
  font-size:13px;line-height:1.65;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
.wrap{max-width:840px;margin:0 auto;padding:32px 28px 56px;}
.report-header{border-bottom:2px solid var(--brand);padding-bottom:14px;margin-bottom:24px;}
.report-header .brand{display:flex;align-items:center;gap:8px;color:var(--brand);font-weight:700;font-size:13px;letter-spacing:.02em;}
.report-header .brand .dot{width:10px;height:10px;border-radius:50%;background:var(--brand);display:inline-block;}
.report-header h1{margin:8px 0 4px;font-size:21px;font-weight:700;}
.report-header .meta{color:var(--muted);font-size:12px;}
h2{font-size:15px;font-weight:700;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line);}
section.narrative{margin-bottom:8px;}
section.narrative p{margin:.5em 0;}
section.narrative ul,section.narrative ol{margin:.5em 0;padding-left:1.4em;}
section.narrative strong{font-weight:700;}
section.narrative code{background:#f1f3f6;border-radius:4px;padding:1px 5px;font-size:12px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
section.narrative h1,section.narrative h2,section.narrative h3{font-size:15px;font-weight:700;margin:1em 0 .4em;border:0;padding:0;}
.chart-block{margin:18px 0 22px;page-break-inside:avoid;break-inside:avoid;}
.chart-title{font-size:14px;font-weight:600;margin:0 0 8px;}
.chart{width:100%;margin-bottom:10px;}
.chart svg{width:100%;height:auto;}
.legend{margin-top:4px;font-size:12px;}
.legend ul{list-style:none;margin:0;padding:0;}
.legend li{display:inline-block;margin:0 8px;}
table{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0;page-break-inside:avoid;}
th,td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top;}
th{background:#f6f8fa;font-weight:600;}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums;}
tbody tr:nth-child(even){background:#fafbfc;}
.appendix pre{background:#0f1115;color:#e6edf3;border-radius:8px;padding:12px 14px;overflow:auto;
  font-size:11.5px;line-height:1.5;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word;}
.report-footer{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:11px;text-align:center;}
.print-hint{margin:0 0 18px;padding:9px 13px;border:1px dashed var(--brand);border-radius:8px;color:var(--brand);font-size:12px;background:#0d94880d;}
@media print{ .print-hint{display:none;} .wrap{padding:0;max-width:none;} @page{margin:16mm;} }
`;

// 최종 리포트 HTML 문서 문자열 조립.
export function buildReportHtml(root: HTMLElement, message: ChatMessage, title: string): string {
  const docEl = document.documentElement;
  const wasDark = docEl.classList.contains('dark');
  // 인쇄에 적합한 라이트 톤으로 고정한 채 차트 색을 인라인 캡처(CSS 변수 기반이라 즉시 반영).
  if (wasDark) docEl.classList.remove('dark');
  let chartsHtml = '';
  try {
    chartsHtml = buildChartsHtml(root, message);
  } finally {
    if (wasDark) docEl.classList.add('dark');
  }

  // 서술 본문: 이미 렌더된 마크다운(시맨틱 태그) innerHTML 을 그대로 — 리포트 CSS 는
  // 태그 기준으로 스타일하므로 Tailwind 클래스 없이도 깔끔히 표시된다.
  const narrative = root.querySelector('[data-report-narrative]')?.innerHTML ?? '';

  const sqls = collectSql(message);
  const appendix = sqls.length
    ? `<section class="appendix"><h2>부록 — 실행된 SQL</h2>${sqls
        .map((s) => `<pre><code>${escapeHtml(s)}</code></pre>`)
        .join('')}</section>`
    : '';

  const when = new Date().toLocaleString('ko-KR', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });

  return `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${REPORT_CSS}</style></head>
<body><div class="wrap">
  <div class="print-hint">이 리포트를 PDF 로 저장하려면 인쇄(⌘P / Ctrl+P) → 대상 “PDF 로 저장”. 차트는 벡터로 보존됩니다.</div>
  <header class="report-header">
    <div class="brand"><span class="dot"></span>AWSome AI Gateway · BI Insight</div>
    <h1>${escapeHtml(title)}</h1>
    <div class="meta">생성 ${escapeHtml(when)} (KST)</div>
  </header>
  <main>
    ${narrative ? `<section class="narrative"><h2>분석 요약</h2>${narrative}</section>` : ''}
    ${chartsHtml ? `<section class="charts"><h2>차트 · 데이터</h2>${chartsHtml}</section>` : ''}
    ${appendix}
  </main>
  <footer class="report-footer">AWSome AI Gateway · BI Insight · 자동 생성 분석 리포트 · 모든 수치는 실행된 SQL 결과 기준</footer>
</div></body></html>`;
}

// 리포트를 새 탭에서 연다. document.write() 대신 **Blob URL** 을 새 탭 src 로 주어
// XSS/파싱 경로를 회피한다(Blob URL 은 same-origin 이라 onload 에서 print() 호출 가능).
// 팝업 차단 시 같은 Blob 을 HTML 파일로 다운로드(열어서 인쇄→PDF 가능).
export function exportMessageReport(root: HTMLElement, message: ChatMessage, title?: string): void {
  const reportTitle = title?.trim() || 'BI Insight 분석 리포트';
  const html = buildReportHtml(root, message, reportTitle);

  const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
  const url = URL.createObjectURL(blob);

  const win = window.open(url, '_blank', 'noopener,noreferrer');
  if (!win) {
    // 팝업 차단 → 자체완결 HTML 파일로 다운로드.
    const a = document.createElement('a');
    a.href = url;
    a.download = `${reportTitle}.html`;
    a.click();
    // 다운로드 큐가 URL 을 잡을 시간을 준 뒤 해제.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    return;
  }
  // same-origin Blob 문서가 로드되면 인쇄 다이얼로그(→ PDF 저장). 차트는 벡터 보존.
  win.addEventListener('load', () => {
    setTimeout(() => {
      try {
        win.print();
      } catch {
        /* 사용자가 수동 인쇄 가능 — 무시 */
      }
    }, 300);
  });
  // 새 탭이 Blob 을 가져간 뒤 URL 해제(즉시 revoke 하면 about:blank 가 됨).
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
