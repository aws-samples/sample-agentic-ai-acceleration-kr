// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Rate Limits 화면의 설정 패널. 이 컴포넌트에는 테스트가 **하나도 없었고**, 그게 아래
 * 결함들이 조용히 살아 있던 이유다.
 *
 * 1. 상속값 프리필 → 저장 시 굳음
 *    서버는 USER 행이 없는 사용자에게 팀의 행을 `config`(scope="TEAM") + `inherited_from` 으로
 *    실어 보낸다("실효값"). 패널은 `node?.config` 존재만 보고 그 값을 입력칸에 채웠다. PUT 은
 *    전체 교체이므로 CPH 하나만 고쳐 저장해도 팀의 RPM/TPM 이 **새 USER 행**으로 굳는다.
 *    작성자의 원래 의도는 `inheritedRpm`/`inheritedTpm` = **placeholder** 였는데, 프리필이 그
 *    의도를 도달 불가 코드로 만들어 두었다.
 * 2. "최근 60초간 요청 없음" 의 거짓 단정 — `measured` 로 구분한다.
 * 3. cpm/cph 의 와이어 타입 — Decimal 은 JSON 문자열("0.5000"), 폼은 숫자로 보내야 한다.
 * 4. 되돌릴 방법 — 입력을 비우고 저장하는 것은 제거가 아니다. 제거 버튼은 자기 행이 있을 때만.
 * 5. GLOBAL 은 모델별이다(admin-api 0046) — 노드마다 따로 저장되고, 다른 모델의 한도를 이유로 경고·확인하지
 *    않는다(단일 슬롯 시절의 '해제 동의' release_alias 도 보내지 않는다 — admin-api 는 그 값에 409 다).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { RateLimitTreeNode } from '@/types/entities';
import type { RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import treeFixture from '../../fixtures/rate-limit-tree.json';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}:${Object.values(values).join(',')}` : key,
}));

const setRateLimitAction = vi.fn();
const deleteUserRateLimitAction = vi.fn();
vi.mock('@/lib/actions/rate-limits', () => ({
  setRateLimitAction: (...a: unknown[]) => setRateLimitAction(...a),
  deleteUserRateLimitAction: (...a: unknown[]) => deleteUserRateLimitAction(...a),
}));

const fetchRateLimitUsage = vi.fn();
vi.mock('@/lib/utils/rateLimitUsage', () => ({
  fetchRateLimitUsage: (...a: unknown[]) => fetchRateLimitUsage(...a),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

import { RateLimitConfigPanel } from '@/components/rate-limits/RateLimitConfigPanel';

/**
 * 픽스처는 admin-api 가 **직접 직렬화한 트리**다(admin-api tests/unit/test_rate_limit_tree.py
 * ::test_wire_matches_admin_ui_fixture 가 같은 파일과 완전 일치를 단정한다). 손으로 베낀 노드를
 * 쓰면 서버가 모양을 바꿔도 이 테스트가 계속 통과해 버리므로, 반드시 이 파일에서 읽는다.
 */
const nodes = treeFixture as unknown as RateLimitTreeNode[];
const globalCapped = nodes.find((n) => n.scope === 'GLOBAL' && n.config)!;
const globalUncapped = nodes.find((n) => n.scope === 'GLOBAL' && !n.config)!;
const teamNode = nodes.find((n) => n.scope === 'TEAM')!;
const ownUserNode = teamNode.children.find((c) => !c.inherited_from)!; // Alice
const inheritingNode = teamNode.children.find((c) => c.inherited_from)!; // Bob
const teamlessNode = nodes.find((n) => n.scope === 'USER')!; // Zoe (config: null)

const usage = (over: Partial<RateLimitUsage> = {}): RateLimitUsage => ({
  available: true,
  measured: true,
  limit_rpm: null,
  window_sec: 60,
  rpm_used_total: 0,
  by_model: [],
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  setRateLimitAction.mockResolvedValue({ success: true, data: undefined });
  deleteUserRateLimitAction.mockResolvedValue({ success: true, data: undefined });
  fetchRateLimitUsage.mockResolvedValue(usage({ available: false, measured: false }));
});

const rpmInput = () => screen.getByLabelText('RPM (Requests Per Minute)');
const tpmInput = () => screen.getByLabelText('TPM (Tokens Per Minute)');
const cpmInput = () => screen.getByLabelText('CPM (Cost Per Minute, USD)');
const cphInput = () => screen.getByLabelText('CPH (Cost Per Hour, USD)');

describe('상속 중인 USER 노드', () => {
  it('상속값을 입력칸에 채우지 않고 placeholder 로만 보여준다', async () => {
    render(<RateLimitConfigPanel node={inheritingNode} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalled());

    expect(inheritingNode.config?.rpm).toBe(100); // 픽스처 전제(팀 값)
    expect(rpmInput()).toHaveValue(null);
    expect(tpmInput()).toHaveValue(null);
    expect(rpmInput()).toHaveAttribute('placeholder', '100');
    expect(tpmInput()).toHaveAttribute('placeholder', '9000');
    // cpm/cph 도 같은 규칙 — 와이어는 Decimal 문자열이다.
    expect(cpmInput()).toHaveValue(null);
    expect(cpmInput()).toHaveAttribute('placeholder', '0.5000');
    expect(cphInput()).toHaveAttribute('placeholder', '12.3456');
    // 상속 중에는 "비워두면 제한 없음" 이 거짓이다 — 비워두면 팀 한도가 계속 적용된다.
    expect(screen.getAllByText('emptyMeansInherit')).toHaveLength(2);
    expect(screen.queryByText('emptyMeansUnlimited')).toBeNull();
  });

  it('CPH 만 입력해 저장하면 팀의 RPM/TPM 이 USER 행으로 굳지 않는다', async () => {
    const user = userEvent.setup();
    render(<RateLimitConfigPanel node={inheritingNode} />);

    await user.type(cphInput(), '7.5');
    await user.click(screen.getByRole('button', { name: 'save' }));

    await waitFor(() => expect(setRateLimitAction).toHaveBeenCalledTimes(1));
    expect(setRateLimitAction).toHaveBeenCalledWith({
      target_id: inheritingNode.id,
      scope: 'USER',
      rpm: null,
      tpm: null,
      cpm: null,
      cph: 7.5,
    });
  });

  it('개인 한도 제거 버튼은 상속 중일 때 나타나지 않는다', async () => {
    render(<RateLimitConfigPanel node={inheritingNode} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'removeOverride' })).toBeNull();
  });
});

describe('자기 설정이 있는 USER 노드', () => {
  it('Decimal 문자열을 그대로 프리필하고 숫자로 전송한다 (대조군: 자기 값은 프리필된다)', async () => {
    const user = userEvent.setup();
    render(<RateLimitConfigPanel node={ownUserNode} />);

    expect(ownUserNode.config?.cpm).toBe('0.5000'); // 와이어는 문자열이다
    expect(cpmInput()).toHaveValue(0.5); // number 입력이 파싱한 값
    expect(rpmInput()).toHaveValue(60);
    expect(screen.getAllByText('emptyMeansUnlimited')).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: 'save' }));

    await waitFor(() => expect(setRateLimitAction).toHaveBeenCalledTimes(1));
    const payload = setRateLimitAction.mock.calls[0][0];
    expect(payload).toEqual({
      target_id: ownUserNode.id,
      scope: 'USER',
      rpm: 60,
      tpm: 1000,
      cpm: 0.5,
      cph: 12.3456,
    });
    expect(typeof payload.cpm).toBe('number');
  });

  it('개인 한도 제거 버튼이 DELETE 액션을 호출한다', async () => {
    const user = userEvent.setup();
    render(<RateLimitConfigPanel node={ownUserNode} />);

    await user.click(screen.getByRole('button', { name: 'removeOverride' }));

    await waitFor(() => expect(deleteUserRateLimitAction).toHaveBeenCalledWith(ownUserNode.id));
    expect(setRateLimitAction).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringContaining('overrideRemoved') }),
      ),
    );
  });

  it('설정이 아예 없는 사용자에게는 제거 버튼이 없다', async () => {
    render(<RateLimitConfigPanel node={teamlessNode} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'removeOverride' })).toBeNull();
  });
});

describe('GLOBAL 노드', () => {
  it('선택 가능하고 CPM/CPH 는 비활성이며 alias 를 target 으로 저장한다', async () => {
    const user = userEvent.setup();
    render(<RateLimitConfigPanel node={globalCapped} />);

    expect(cpmInput()).toBeDisabled();
    expect(cphInput()).toBeDisabled();
    // TPM 전용 쿼터가 그대로 보인다.
    expect(tpmInput()).toHaveValue(200000);
    expect(rpmInput()).toHaveValue(null);

    await user.click(screen.getByRole('button', { name: 'save' }));

    await waitFor(() => expect(setRateLimitAction).toHaveBeenCalledTimes(1));
    expect(setRateLimitAction).toHaveBeenCalledWith({
      target_id: globalCapped.id, // = model alias, UUID 가 아니다
      scope: 'GLOBAL',
      rpm: null,
      tpm: 200000,
      cpm: undefined,
      cph: undefined,
    });
  });

  it('GLOBAL 도 실시간 사용량을 조회한다 (alias 를 scope_id 로)', async () => {
    render(<RateLimitConfigPanel node={globalCapped} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledWith('GLOBAL', globalCapped.id));
  });

  it('다른 모델에 GLOBAL 한도가 있어도 경고·확인 없이 바로 저장한다 — release_alias 를 보내지 않는다', async () => {
    // 픽스처의 globalCapped(TPM 200000)가 한도를 가진 채로, 한도 없는 모델에 저장한다.
    const user = userEvent.setup();
    render(<RateLimitConfigPanel node={globalUncapped} />);
    await user.type(rpmInput(), '40');

    await user.click(screen.getByRole('button', { name: 'save' }));

    await waitFor(() => expect(setRateLimitAction).toHaveBeenCalledTimes(1));
    expect(setRateLimitAction.mock.calls[0][0]).toEqual({
      target_id: globalUncapped.id,
      scope: 'GLOBAL',
      rpm: 40,
      tpm: null,
      cpm: undefined,
      cph: undefined,
    });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('GLOBAL 노드는 이 모델에만 걸린다고 알린다 — USER/TEAM 노드에는 없다 (대조군)', async () => {
    const { unmount } = render(<RateLimitConfigPanel node={globalUncapped} />);
    expect(screen.getByText(`globalPerModel:${globalUncapped.id}`)).toBeInTheDocument();
    unmount();

    render(<RateLimitConfigPanel node={teamNode} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/^globalPerModel/)).toBeNull();
  });
});

describe('실시간 사용량 표시', () => {
  it('측정 불가(카운터 없음)면 "요청 없음" 을 단정하지 않는다', async () => {
    fetchRateLimitUsage.mockResolvedValue(usage({ measured: false, rpm_used_total: 0 }));
    render(<RateLimitConfigPanel node={teamlessNode} />);

    await waitFor(() => expect(screen.getByText('usageNotMeasured')).toBeInTheDocument());
    expect(screen.queryByText('noRecentRequests:60')).toBeNull();
  });

  it('측정된 0 이면 "요청 없음" 을 보여준다 (대조군)', async () => {
    fetchRateLimitUsage.mockResolvedValue(usage({ measured: true, rpm_used_total: 0, limit_rpm: 60 }));
    render(<RateLimitConfigPanel node={ownUserNode} />);

    await waitFor(() => expect(screen.getByText('noRecentRequests:60')).toBeInTheDocument());
    expect(screen.queryByText('usageNotMeasured')).toBeNull();
  });

  it('상속 노드의 게이지 분모로 팀 한도를 쓰지 않는다', async () => {
    // ⚠️ measured=true 여야 의미가 있다 — measured=false 면 게이지 분기 자체가 안 그려져 분모를 볼
    //    수 없다(예전 이 테스트는 그래서 무엇을 고쳐도 통과했다). 상속 사용자도 USER 카운터가 잡힐 수
    //    있다(예: 방금 지운 개인 한도의 ZSET 이 창 안에 남음). 그래도 자기 rpm 한도는 없다
    //    (limit_rpm=null) — 그 사용량을 팀 한도(100)로 나누면 "USER 스코프 사용률" 이라는 거짓 지표다.
    expect(inheritingNode.config?.rpm).toBe(100); // 전제: 상속 노드의 config 는 팀의 행이다
    fetchRateLimitUsage.mockResolvedValue(usage({ measured: true, limit_rpm: null, rpm_used_total: 7 }));
    const { container } = render(<RateLimitConfigPanel node={inheritingNode} />);

    await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument()); // 측정 분기가 그려졌다
    expect(screen.getByText('7').textContent).toBe('7'); // 분모(" / N") 가 붙지 않았다
    expect(screen.queryByText(/\/ 100/)).toBeNull();
    expect(container.querySelector('[style*="width"]')).toBeNull(); // 게이지 막대도 없다
  });

  it('측정 중이면 자기 한도를 분모로 표시한다', async () => {
    fetchRateLimitUsage.mockResolvedValue(usage({ measured: true, limit_rpm: 60, rpm_used_total: 12 }));
    render(<RateLimitConfigPanel node={ownUserNode} />);

    await waitFor(() => expect(screen.getByText(/\/ 60/)).toBeInTheDocument());
    expect(screen.getByText('12')).toBeInTheDocument();
  });
});

describe('트리를 새로 받아도(revalidatePath) 운영자의 입력과 에러가 남는다', () => {
  // 409 분기(lib/actions/rate-limits.ts)는 지금 상태를 보여 주려고 revalidatePath 로 트리를 다시 받는다.
  // 내용이 같아도 노드는 **새 객체**다(M1 이후 선택은 id 로 매 렌더 새 트리에서 찾는다). 예전 리셋 effect 는
  // 노드 객체에 걸려 있어, 409 가 나는 순간 입력값과 409 문구가 함께 지워졌다.
  const fresh = (id: string): RateLimitTreeNode => {
    const copy = JSON.parse(JSON.stringify(nodes)) as RateLimitTreeNode[];
    const all = copy.flatMap((n) => [n, ...n.children]);
    return all.find((n) => n.id === id)!;
  };
  const CONFLICT = 'The resource was modified concurrently. Please retry.';

  it('409 뒤 같은 노드의 새 객체로 다시 그려도 입력값과 409 문구가 그대로다', async () => {
    const user = userEvent.setup();
    setRateLimitAction.mockResolvedValue({ success: false, error: CONFLICT });
    const { rerender } = render(<RateLimitConfigPanel node={globalUncapped} />);
    await user.type(rpmInput(), '40');
    await user.type(tpmInput(), '7000');
    await user.click(screen.getByRole('button', { name: 'save' }));
    await waitFor(() => expect(screen.getByText(CONFLICT)).toBeInTheDocument());

    rerender(<RateLimitConfigPanel node={fresh(globalUncapped.id)} />);

    expect(rpmInput()).toHaveValue(40);
    expect(tpmInput()).toHaveValue(7000);
    expect(screen.getByText(CONFLICT)).toBeInTheDocument();
  });

  it('다른 노드를 고르면 그 노드의 값으로 바뀌고 에러는 지워진다 (대조군)', async () => {
    const user = userEvent.setup();
    setRateLimitAction.mockResolvedValue({ success: false, error: CONFLICT });
    const { rerender } = render(<RateLimitConfigPanel node={ownUserNode} />);
    await user.clear(rpmInput());
    await user.type(rpmInput(), '99');
    await user.click(screen.getByRole('button', { name: 'save' }));
    await waitFor(() => expect(screen.getByText(CONFLICT)).toBeInTheDocument());

    rerender(<RateLimitConfigPanel node={fresh(teamlessNode.id)} />);

    expect(rpmInput()).toHaveValue(null);
    expect(screen.queryByText(CONFLICT)).toBeNull();
  });

  it('같은 노드라도 자기 저장값이 바뀌면 입력칸이 새 값을 따른다 (대조군)', async () => {
    const { rerender } = render(<RateLimitConfigPanel node={ownUserNode} />);
    expect(rpmInput()).toHaveValue(60);

    const saved = fresh(ownUserNode.id);
    saved.config = { ...saved.config!, rpm: 61 };
    rerender(<RateLimitConfigPanel node={saved} />);

    expect(rpmInput()).toHaveValue(61);
  });

  it('새로 받은 트리에서 이 노드의 저장값이 바뀌었으면 409 문구도 지운다 — 그 409 는 이제 낡았다', async () => {
    // 예전엔 입력칸은 새 저장값(77)으로 바뀌는데 409 문구는 남아, 운영자는 지금 값에 대한 충돌로 읽었다.
    const user = userEvent.setup();
    setRateLimitAction.mockResolvedValue({ success: false, error: CONFLICT });
    const { rerender } = render(<RateLimitConfigPanel node={globalUncapped} />);
    await user.type(rpmInput(), '40');
    await user.click(screen.getByRole('button', { name: 'save' }));
    await waitFor(() => expect(screen.getByText(CONFLICT)).toBeInTheDocument());

    const changed = fresh(globalUncapped.id); // 다른 운영자가 이 모델에 rpm 77 을 저장했다
    changed.config = { target_id: changed.id, scope: 'GLOBAL', rpm: 77, tpm: null, cpm: null, cph: null };
    rerender(<RateLimitConfigPanel node={changed} />);

    expect(rpmInput()).toHaveValue(77);
    expect(screen.queryByText(CONFLICT)).toBeNull();
  });

  it('노드가 트리에서 사라지면 선택 안내로 돌아가고, 다시 나타나면 저장값으로 시작한다', async () => {
    const user = userEvent.setup();
    setRateLimitAction.mockResolvedValue({ success: false, error: CONFLICT });
    const { rerender } = render(<RateLimitConfigPanel node={ownUserNode} />);
    await user.clear(rpmInput());
    await user.type(rpmInput(), '99');
    await user.click(screen.getByRole('button', { name: 'save' }));
    await waitFor(() => expect(screen.getByText(CONFLICT)).toBeInTheDocument());

    rerender(<RateLimitConfigPanel node={null} />); // 예: 비활성 필터로 트리에서 빠졌다
    expect(screen.getByText('selectNodePrompt')).toBeInTheDocument();
    expect(screen.queryByText(CONFLICT)).toBeNull();

    rerender(<RateLimitConfigPanel node={fresh(ownUserNode.id)} />); // 다시 나타났다
    expect(rpmInput()).toHaveValue(60); // 사라지기 전의 입력(99)이 아니라 저장값
    expect(screen.queryByText(CONFLICT)).toBeNull();
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenLastCalledWith('USER', ownUserNode.id));
  });

  it('저장으로 이 노드의 RPM 이 바뀌면 사용량을 바로 다시 불러 게이지 분모가 새 한도를 따른다', async () => {
    // 폴링이 (scope, id) 에만 걸려 있던 동안, 저장 뒤 입력칸은 999 인데 게이지는 다음 주기(최대 10초)까지 서버의
    // 옛 limit_rpm 으로 "/ 60" 을 보였다.
    fetchRateLimitUsage.mockResolvedValue(usage({ measured: true, limit_rpm: 60, rpm_used_total: 12 }));
    const { rerender } = render(<RateLimitConfigPanel node={ownUserNode} />);
    await waitFor(() => expect(screen.getByText(/\/ 60/)).toBeInTheDocument());

    fetchRateLimitUsage.mockResolvedValue(usage({ measured: true, limit_rpm: 999, rpm_used_total: 12 }));
    const saved = fresh(ownUserNode.id);
    saved.config = { ...saved.config!, rpm: 999 };
    rerender(<RateLimitConfigPanel node={saved} />);

    expect(rpmInput()).toHaveValue(999);
    await waitFor(() => expect(screen.getByText(/\/ 999/)).toBeInTheDocument());
    expect(screen.queryByText(/\/ 60/)).toBeNull();
    expect(fetchRateLimitUsage).toHaveBeenCalledTimes(2);
  });

  it('같은 노드를 새로 받은 트리로 다시 그려도 사용량 폴링을 처음부터 다시 시작하지 않는다', async () => {
    const { rerender } = render(<RateLimitConfigPanel node={ownUserNode} />);
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledTimes(1));

    rerender(<RateLimitConfigPanel node={fresh(ownUserNode.id)} />);
    rerender(<RateLimitConfigPanel node={fresh(ownUserNode.id)} />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchRateLimitUsage).toHaveBeenCalledTimes(1); // 폴링 주기(10초)가 아니라 새로고침마다 다시 불렀다

    rerender(<RateLimitConfigPanel node={teamlessNode} />); // 대조군 — 다른 노드면 새로 시작한다
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledTimes(2));
    expect(fetchRateLimitUsage).toHaveBeenLastCalledWith('USER', teamlessNode.id);
  });
});
