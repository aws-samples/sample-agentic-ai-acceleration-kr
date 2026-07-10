// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { escapeHtml, tableHtml, buildReportHtml } from '@/components/chat/reportExport';
import type { ChatMessage } from '@/components/chat/types';

// reportExport 의 순수 HTML 조립 로직 검증(jsdom). 차트 SVG 캡처는 라이브 recharts
// DOM 이 필요해 e2e 영역이므로 여기선 이스케이프·표·부록·narrative 추출에 집중.

describe('escapeHtml', () => {
  it('escapes HTML metacharacters (XSS 방어)', () => {
    expect(escapeHtml('<script>alert("x")</script>')).toBe(
      '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;',
    );
    expect(escapeHtml("a & b ' c")).toBe('a &amp; b &#39; c');
  });

  it('renders null/undefined as empty string', () => {
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
  });
});

describe('tableHtml', () => {
  it('returns empty string for empty/undefined data', () => {
    expect(tableHtml(undefined)).toBe('');
    expect(tableHtml([])).toBe('');
  });

  it('renders headers and rows, right-aligning numeric columns', () => {
    const html = tableHtml([
      { team: 'search', cost: 1234.5 },
      { team: 'chat', cost: 987.6 },
    ]);
    expect(html).toContain('<th class="">team</th>');
    expect(html).toContain('<th class="num">cost</th>'); // 숫자 컬럼 우측정렬
    expect(html).toContain('<td class="num">1234.5</td>');
    expect(html).toContain('<td class="">search</td>');
  });

  it('escapes cell content (injection 방어)', () => {
    const html = tableHtml([{ name: '<b>x</b>' }]);
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;');
    expect(html).not.toContain('<b>x</b>');
  });

  it('JSON-stringifies object cells', () => {
    const html = tableHtml([{ meta: { a: 1 } }]);
    expect(html).toContain('{&quot;a&quot;:1}');
  });
});

describe('buildReportHtml', () => {
  function makeRoot(narrativeHtml: string): HTMLElement {
    const root = document.createElement('div');
    const narrative = document.createElement('div');
    narrative.setAttribute('data-report-narrative', '');
    narrative.innerHTML = narrativeHtml;
    root.appendChild(narrative);
    return root;
  }

  const baseMsg: ChatMessage = {
    id: 'm1',
    role: 'assistant',
    content: '이번 달 총비용 분석',
  };

  it('embeds the rendered narrative HTML and the title', () => {
    const root = makeRoot('<p>총비용은 <strong>$28</strong> 입니다.</p>');
    const html = buildReportHtml(root, baseMsg, '6월 비용 리포트');
    expect(html).toContain('<title>6월 비용 리포트</title>');
    expect(html).toContain('총비용은 <strong>$28</strong> 입니다.');
    expect(html).toContain('분석 요약'); // narrative 섹션 헤더
  });

  it('appends executed SQL from verifications/toolCalls as an appendix', () => {
    const root = makeRoot('<p>x</p>');
    const msg: ChatMessage = {
      ...baseMsg,
      verifications: [
        {
          method: 'execution_self_consistency',
          k: 3,
          n_valid: 3,
          agreement: 1,
          n_clusters: 1,
          tie: false,
          verdict: 'PASS',
          chosen_sql: 'SELECT 1 FROM usage_logs',
        },
      ],
      toolCalls: [
        { tool: 'ask_sql_specialist', result: { sql: 'SELECT 2 FROM teams' } },
      ],
    };
    const html = buildReportHtml(root, msg, 'T');
    expect(html).toContain('부록 — 실행된 SQL');
    expect(html).toContain('SELECT 1 FROM usage_logs');
    expect(html).toContain('SELECT 2 FROM teams');
  });

  it('omits the SQL appendix when no SQL is present', () => {
    const root = makeRoot('<p>x</p>');
    const html = buildReportHtml(root, baseMsg, 'T');
    expect(html).not.toContain('부록 — 실행된 SQL');
  });

  it('is a self-contained document with inline styles (no external deps)', () => {
    const root = makeRoot('<p>x</p>');
    const html = buildReportHtml(root, baseMsg, 'T');
    expect(html.startsWith('<!doctype html>')).toBe(true);
    expect(html).toContain('<style>');
    expect(html).not.toContain('<link rel="stylesheet"'); // 외부 CSS 의존 없음
  });
});
