// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /budgets — TEAM_LEADER 화면은 admin-api 의 예산 읽기 범위가 좁아진 뒤에도 그대로 그려진다.
 *
 * admin-api 는 이제 리더에게 **자기 팀으로 걸러진** 예산 요약을 주고(전사 요약이 나가던 결함), 다른 팀의
 * 배정·자동 하향은 403 이다(admin-api tests/regression/test_high_console_contract_real_db.py 11절이 실제
 * 라우터로 증명한다). 이 페이지는 리더에게도 요약을 받아 오지만 그리는 것은 자기 팀 배정(TeamAllocationView)
 * 이다 — 걸러진 요약에서도, 요약 호출이 아예 실패해도 그 화면이 나와야 한다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const { OWN_TEAM } = vi.hoisted(() => ({ OWN_TEAM: '00000000-0000-4000-a000-000000000003' }));

vi.mock('next/headers', () => ({
  cookies: () => ({ get: () => ({ value: 'jwt' }), toString: () => '' }),
}));
vi.mock('next-intl/server', () => ({ getTranslations: async () => (key: string) => key }));
// 페이지는 lib/serverSession.ts → resolveSession 으로 세션을 읽는다(역할 쿠키 포함).
vi.mock('@/lib/auth', () => ({
  resolveSession: async () => ({ role: 'TEAM_LEADER', team_id: OWN_TEAM }),
}));

const get = vi.fn();
vi.mock('@/lib/api-client', () => ({ adminAPI: { get: (...a: unknown[]) => get(...a) } }));

vi.mock('@/components/budgets/BudgetSummaryTable', () => ({
  BudgetSummaryTable: () => <div data-testid="summary-table" />,
}));
vi.mock('@/components/budgets/TeamAllocationView', () => ({
  TeamAllocationView: ({
    teamId,
    initialAllocation,
  }: {
    teamId: string;
    initialAllocation: { entries: unknown[] } | null;
  }) => (
    <div data-testid="team-allocation">
      {teamId}:{initialAllocation ? initialAllocation.entries.length : 'none'}
    </div>
  ),
}));
vi.mock('@/components/budgets/DowngradeSection', () => ({
  DowngradeSection: () => <div data-testid="downgrade" />,
}));
vi.mock('@/components/chat/RegisterScreenContext', () => ({ RegisterScreenContext: () => null }));

import BudgetsPage from '@/app/budgets/page';

const MEMBER = 'aaaa0000-0000-4000-a000-0000000000a1';

const ALLOCATION = {
  team_id: OWN_TEAM,
  team_name: 'Own Team',
  total_budget_usd: '5000.0000',
  entries: [
    { target_id: OWN_TEAM, target_name: 'Own Team', target_type: 'TEAM', allocated_usd: '5000.0000',
      used_usd: '12.0000', remaining_usd: '4988.0000', alert_level: 'NORMAL' },
    { target_id: MEMBER, target_name: 'Own Member', target_type: 'USER', allocated_usd: '10.0000',
      used_usd: '1.0000', remaining_usd: '9.0000', alert_level: 'NORMAL' },
  ],
};

// admin-api 가 리더에게 주는 요약의 모양 — 자기 팀 행과 그 멤버 행뿐이다.
const LEADER_SUMMARY = {
  period: '2026-10',
  summary: [
    { target_type: 'team', target_id: OWN_TEAM, target_name: 'Own Team', team_id: null, is_active: true,
      limit_usd: '5000.0000', used_usd: '12.0000', remaining_usd: '4988.0000', usage_pct: '0.24',
      department_id: null, department_name: null, alert_thresholds: [80, 90, 100] },
    { target_type: 'user', target_id: MEMBER, target_name: 'Own Member', team_id: OWN_TEAM, is_active: true,
      limit_usd: '10.0000', used_usd: '1.0000', remaining_usd: '9.0000', usage_pct: '10.00',
      department_id: null, department_name: null, alert_thresholds: [80, 90, 100] },
  ],
};

function serve(summary: () => Promise<unknown>) {
  get.mockImplementation(async (path: string) => {
    if (path === '/admin/budgets/summary') return summary();
    if (path === `/admin/budgets/team/${OWN_TEAM}/allocation`) return ALLOCATION;
    if (path === '/admin/models') return { items: [] };
    throw new Error(`unexpected request: ${path}`);
  });
}

const requested = () => get.mock.calls.map(([path]) => String(path));

beforeEach(() => {
  get.mockReset();
});

describe('BudgetsPage — TEAM_LEADER', () => {
  it('자기 팀으로 걸러진 요약을 받아도 자기 팀 배정 화면을 그린다', async () => {
    serve(async () => LEADER_SUMMARY);
    render(await BudgetsPage());

    expect(screen.getByTestId('team-allocation')).toHaveTextContent(`${OWN_TEAM}:2`);
    expect(screen.queryByTestId('summary-table')).toBeNull(); // 관리자 표는 그리지 않는다
    expect(screen.queryByTestId('downgrade')).toBeNull();
    // 리더 화면이 부르는 배정은 자기 팀 것 하나뿐이다 — 다른 팀 배정(이제 403)을 부르지 않는다.
    expect(requested().filter((p) => p.includes('/allocation'))).toEqual([
      `/admin/budgets/team/${OWN_TEAM}/allocation`,
    ]);
  });

  it('요약 호출이 실패해도(403 등) 화면은 그대로다', async () => {
    serve(async () => {
      throw Object.assign(new Error('Forbidden'), { status: 403 });
    });
    render(await BudgetsPage());

    expect(screen.getByTestId('team-allocation')).toHaveTextContent(`${OWN_TEAM}:2`);
  });
});
