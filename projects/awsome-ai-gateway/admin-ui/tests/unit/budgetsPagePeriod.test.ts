// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /budgets 페이지가 요청하는 기간은 **KST 월**이어야 한다.
 *
 * 결함: 페이지가 `new Date().getMonth()` 로 기간을 만들었다. 서버 컴포넌트라 그 "로컬" 은
 * Next 컨테이너의 UTC 이고, 백엔드 집계는 KST 버킷(§59)이다. 그래서 매월 1일 KST 00:00~09:00
 * 동안 /admin/budgets/summary 에 **지난달**을 물었다 — 새 달 첫 아침에 지난달 소진율이 그대로 보인다.
 *
 * 경계 시각을 고정해 재현한다: TZ=UTC, 2026-09-30T18:00:00Z = KST 2026-10-01 03:00.
 * 회귀하면 '2026-09' 가 나가므로 반드시 red 가 된다.
 */

process.env.TZ = 'UTC';

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('next/headers', () => ({
  cookies: () => ({ get: () => ({ value: 'jwt' }), toString: () => '' }),
}));
vi.mock('next-intl/server', () => ({ getTranslations: async () => (key: string) => key }));
// 페이지는 lib/serverSession.ts → resolveSession 으로 세션을 읽는다(역할 쿠키 포함).
vi.mock('@/lib/auth', () => ({ resolveSession: async () => ({ role: 'ADMIN', team_id: null }) }));

const get = vi.fn();
vi.mock('@/lib/api-client', () => ({ adminAPI: { get: (...a: unknown[]) => get(...a) } }));

// 렌더하지 않는다 — 페이지 함수가 무엇을 **요청**하는지만 본다.
vi.mock('@/components/budgets/BudgetSummaryTable', () => ({ BudgetSummaryTable: () => null }));
vi.mock('@/components/budgets/TeamAllocationView', () => ({ TeamAllocationView: () => null }));
vi.mock('@/components/budgets/DowngradeSection', () => ({ DowngradeSection: () => null }));
vi.mock('@/components/chat/RegisterScreenContext', () => ({ RegisterScreenContext: () => null }));

import BudgetsPage from '@/app/budgets/page';

function summaryPeriod(): string | undefined {
  const call = get.mock.calls.find(([path]) => path === '/admin/budgets/summary');
  return (call?.[1] as { period?: string } | undefined)?.period;
}

beforeEach(() => {
  get.mockReset();
  get.mockImplementation(async (path: string) =>
    path === '/admin/budgets/summary' ? { summary: [] } : { items: [] },
  );
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('BudgetsPage period', () => {
  it('KST 월 첫 9시간(UTC 로는 아직 전월)에 새 달을 요청한다', async () => {
    vi.setSystemTime(new Date('2026-09-30T18:00:00Z')); // = KST 2026-10-01 03:00
    expect(new Date().getMonth() + 1).toBe(9); // 전제: 프로세스 로컬(UTC)로는 아직 9월
    await BudgetsPage();
    expect(summaryPeriod()).toBe('2026-10');
  });

  it('KST 로도 같은 달이면 그 달이다 (대조군)', async () => {
    vi.setSystemTime(new Date('2026-09-30T14:59:59Z')); // = KST 2026-09-30 23:59:59
    await BudgetsPage();
    expect(summaryPeriod()).toBe('2026-09');
  });
});
