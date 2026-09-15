// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 예산 다이얼로그가 **저장된** 알림 임계값을 보여주는지.
 *
 * 배경: 이 기능은 UI 부터 Lua 까지 다 있었는데 저장소만 없었다(migration 0037 이 추가한
 * `budget_configs.alert_thresholds`). 두 방향이 끊겨 있었고 이 파일은 읽기 쪽을 고정한다.
 *
 *   쓰기: 값이 DB 에 저장되지 않아 Redis 설정 키의 TTL(300초)만큼만 살아 있었다.
 *   읽기: 예산 요약 응답에 이 필드가 없어서 다이얼로그가 **항상** [80,90,100] 으로
 *         초기화됐다 — 50% 를 저장하고 다시 열면 저장한 값이 사라진 것처럼 보인다.
 *
 * ⚠️ 이 증상이 특히 나쁜 이유: 화면이 비어 있지 않고 **그럴듯한 기본값**을 보여준다.
 *    운영자는 자기가 설정한 값이 지워졌다고 판단해 다시 입력하고, 그 값도 5분 뒤 사라진다.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SetBudgetDialog } from '@/components/budgets/SetBudgetDialog';

vi.mock('@/lib/actions/budgets', () => ({
  setBudgetAction: vi.fn(),
  deleteUserBudgetAction: vi.fn(),
}));
// ⚠️ 실제 export 이름과 맞춰야 한다. 처음에 이름을 틀렸더니 다이얼로그의 useEffect 가
//    undefined 를 호출해 unhandled rejection 이 4건 났다 — 테스트는 "통과" 로 보이지만
//    CI 는 unhandled error 로 실패할 수 있다.
vi.mock('@/lib/actions/users', () => ({
  getUserAllowedClientsAction: vi.fn(async () => ({ success: true, data: { clients: [] } })),
  getUserClientBudgetsAction: vi.fn(async () => ({ success: true, data: { apps: [] } })),
  setUserClientBudgetAction: vi.fn(),
  clearUserClientBudgetAction: vi.fn(),
}));
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const BASE_TARGET = {
  id: '11111111-1111-1111-1111-111111111111',
  name: 'alice',
  type: 'USER' as const,
  currentLimit: 100,
};

/** 화면에 렌더된 임계값 칩("50%" 등)을 숫자로 걷어 온다. */
function renderedThresholds(): number[] {
  return screen
    .getAllByText(/^\d+%$/)
    .map(el => parseInt(el.textContent!.replace('%', ''), 10))
    .sort((a, b) => a - b);
}

describe('SetBudgetDialog — 저장된 임계값 표시', () => {
  it('저장된 값이 있으면 그것으로 초기화한다', () => {
    render(
      <SetBudgetDialog
        isOpen
        onClose={() => {}}
        target={{ ...BASE_TARGET, alertThresholds: [50, 75] }}
      />,
    );
    expect(renderedThresholds()).toEqual([50, 75]);
  });

  it('저장된 값이 없으면(예산 미설정) 표준 기본값으로 초기화한다', () => {
    // ⚠️ 대조군. 이것이 없으면 초기화를 아예 빈 배열로 바꿔도 위 테스트가 통과하고,
    //    새 예산을 만들 때 임계값이 하나도 없는 상태로 시작한다(UI 는 최소 1개를 요구하므로
    //    저장 자체가 막힌다).
    render(
      <SetBudgetDialog isOpen onClose={() => {}} target={{ ...BASE_TARGET }} />,
    );
    expect(renderedThresholds()).toEqual([80, 90, 100]);
  });

  it('null 이 오면 기본값을 쓴다 — 예산 미설정 대상은 서버가 null 을 준다', () => {
    render(
      <SetBudgetDialog
        isOpen
        onClose={() => {}}
        target={{ ...BASE_TARGET, alertThresholds: null }}
      />,
    );
    expect(renderedThresholds()).toEqual([80, 90, 100]);
  });

  it('저장된 값이 기본값과 겹치지 않아도 기본값이 섞여 들어오지 않는다', () => {
    // ⚠️ `[...DEFAULT, ...saved]` 같은 병합으로 "고치면" 운영자가 지운 80% 가 되살아난다.
    render(
      <SetBudgetDialog
        isOpen
        onClose={() => {}}
        target={{ ...BASE_TARGET, alertThresholds: [42] }}
      />,
    );
    expect(renderedThresholds()).toEqual([42]);
  });
});
