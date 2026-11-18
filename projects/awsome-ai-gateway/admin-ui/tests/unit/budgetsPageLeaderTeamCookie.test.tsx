// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /budgets — SSO 로 들어온 팀 리더는 역할 쿠키에 기억한 팀으로 자기 팀 배정을 본다.
 *
 * 결함: IdP 토큰에는 team_id 가 없다. 역할 쿠키가 역할만 기억하던 동안 팀 리더는 이 화면에 들어와도
 * `session.team_id` 가 비어 "배정된 팀이 없습니다." 만 보였다(실 IdP·실 admin-api 에서 확인). 이제 쿠키가
 * `/admin/me` 의 team_id 도 기억한다.
 *
 * budgetsPageLeader.test.tsx 와 달리 세션을 흉내 내지 않는다 — 진짜 토큰과 진짜 역할 쿠키를 next/headers
 * 로 넣어 lib/serverSession → lib/auth → lib/roleCookie 를 그대로 태운다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const { OWN_TEAM, jar } = vi.hoisted(() => ({
  OWN_TEAM: '00000000-0000-4000-a000-000000000003',
  jar: new Map<string, string>(),
}));

vi.mock('next/headers', () => ({
  cookies: () => ({
    get: (name: string) => (jar.has(name) ? { name, value: jar.get(name)! } : undefined),
    toString: () => '',
  }),
}));
vi.mock('next-intl/server', () => ({ getTranslations: async () => (key: string) => key }));

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
import { encodeRoleCookie } from '@/lib/roleCookie';

function token(payload: Record<string, unknown>): string {
  return `header.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.sig`;
}

/** IdP id_token 모양 — role 도 team_id 도 없다. */
const IDP_TOKEN = token({ sub: 'idp-subject', email: 'lead@example.com', groups: ['Developers'] });

const ALLOCATION = {
  team_id: OWN_TEAM,
  team_name: 'Own Team',
  total_budget_usd: '5000.0000',
  entries: [
    { target_id: OWN_TEAM, target_name: 'Own Team', target_type: 'TEAM', allocated_usd: '5000.0000',
      used_usd: '12.0000', remaining_usd: '4988.0000', alert_level: 'NORMAL' },
    { target_id: 'aaaa0000-0000-4000-a000-0000000000a1', target_name: 'Own Member', target_type: 'USER',
      allocated_usd: '10.0000', used_usd: '1.0000', remaining_usd: '9.0000', alert_level: 'NORMAL' },
  ],
};

const allocationRequests = () =>
  get.mock.calls.map(([path]) => String(path)).filter((p) => p.includes('/allocation'));

beforeEach(() => {
  get.mockReset();
  get.mockImplementation(async (path: string) => {
    if (path === '/admin/budgets/summary') return { summary: [] };
    if (path === `/admin/budgets/team/${OWN_TEAM}/allocation`) return ALLOCATION;
    if (path === '/admin/models') return { items: [] };
    throw new Error(`unexpected request: ${path}`);
  });
  jar.clear();
  jar.set('admin_jwt', IDP_TOKEN);
});

describe('BudgetsPage — SSO 팀 리더', () => {
  it('역할 쿠키의 팀으로 자기 팀 배정을 요청하고 그린다', async () => {
    jar.set('admin_role', await encodeRoleCookie('TEAM_LEADER', IDP_TOKEN, OWN_TEAM));

    render(await BudgetsPage());

    expect(allocationRequests()).toEqual([`/admin/budgets/team/${OWN_TEAM}/allocation`]);
    expect(screen.getByTestId('team-allocation')).toHaveTextContent(`${OWN_TEAM}:2`);
    expect(screen.queryByText('noTeamAssigned')).toBeNull();
    expect(screen.queryByTestId('summary-table')).toBeNull(); // 관리자 표는 그리지 않는다
  });

  it('대조군: 쿠키에 팀이 없으면 "팀 없음" 이고 배정을 요청하지 않는다', async () => {
    // 위 단정이 공허하지 않다는 증거 — 팀은 토큰이 아니라 쿠키에서 왔다.
    jar.set('admin_role', await encodeRoleCookie('TEAM_LEADER', IDP_TOKEN));

    render(await BudgetsPage());

    expect(allocationRequests()).toEqual([]);
    expect(screen.getByText('noTeamAssigned')).toBeInTheDocument();
    expect(screen.queryByTestId('team-allocation')).toBeNull();
  });

  it('다른 토큰에 묶인 쿠키의 팀은 쓰지 않는다', async () => {
    const other = token({ sub: 'someone-else', groups: ['Developers'] });
    jar.set('admin_role', await encodeRoleCookie('TEAM_LEADER', other, OWN_TEAM));

    render(await BudgetsPage());

    expect(allocationRequests()).toEqual([]);
    expect(screen.getByText('noTeamAssigned')).toBeInTheDocument();
  });
});
