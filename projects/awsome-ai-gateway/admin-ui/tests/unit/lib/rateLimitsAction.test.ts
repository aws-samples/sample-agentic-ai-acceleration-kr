// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * rate-limits 서버 액션의 URL 조립 + 실시간 사용량 응답 정규화.
 *
 * 트리의 GLOBAL 노드 id 는 UUID 가 아니라 **model alias** 다(그래서 PUT
 * `/admin/rate-limits/global/{model_alias}` 에 도달할 수 있다). alias 에는 URL 경로에서 인코딩이
 * 필요한 문자가 실제로 들어 있다 — 이 레포에 `claude-sonnet-4-6[1m]` 가 있다.
 *
 * DELETE 는 "개인 한도 제거" 경로다. PUT 이 전체 교체이므로 값을 비우고 저장하는 것은 전부
 * NULL 인 USER 행을 남기는 것이고, 그러면 트리에서 상속 표식이 사라진다.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('next/cache', () => ({ revalidatePath: vi.fn() }));

const putMock = vi.fn().mockResolvedValue({});
const deleteMock = vi.fn().mockResolvedValue({});
vi.mock('@/lib/api-client', () => ({
  adminAPI: {
    put: (...args: unknown[]) => putMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
}));

import { setRateLimitAction, deleteUserRateLimitAction } from '@/lib/actions/rate-limits';
import { revalidatePath } from 'next/cache';
import { APIError } from '@/lib/utils/retry';
import { fetchRateLimitUsage } from '@/lib/utils/rateLimitUsage';

beforeEach(() => {
  putMock.mockClear();
  deleteMock.mockClear();
});

describe('setRateLimitAction', () => {
  it('GLOBAL alias 의 URL 메타문자를 인코딩한다', async () => {
    const res = await setRateLimitAction({
      target_id: 'claude-sonnet-4-6[1m]',
      scope: 'GLOBAL',
      rpm: 60,
      tpm: 200000,
    });

    expect(res.success).toBe(true);
    expect(putMock).toHaveBeenCalledWith('/admin/rate-limits/global/claude-sonnet-4-6%5B1m%5D', {
      rpm: 60,
      tpm: 200000,
    });
  });

  it("'/' 가 든 alias 는 %2F 로 보낸다 — admin-api 는 `{model_alias:path}` 로 받는다", async () => {
    await setRateLimitAction({ target_id: 'us.anthropic/claude-x', scope: 'GLOBAL', rpm: 1 });
    expect(putMock.mock.calls[0][0]).toBe('/admin/rate-limits/global/us.anthropic%2Fclaude-x');
  });

  it('release_alias 를 넘겨도 보내지 않는다 — GLOBAL 은 모델별이고 admin-api 는 그 값에 409 다', async () => {
    // 단일 슬롯 시절의 '해제 동의' 다. 스키마에 없으므로 버려지고 URL 에도 본문에도 실리지 않는다.
    // 대조군: 단일 슬롯 시절의 액션은 `?release_alias=...` 를 붙였다.
    await setRateLimitAction({
      target_id: 'claude-opus-4-7',
      scope: 'GLOBAL',
      rpm: 5,
      release_alias: 'claude-sonnet-4-6[1m]',
    });
    expect(putMock).toHaveBeenCalledWith('/admin/rate-limits/global/claude-opus-4-7', { rpm: 5 });
  });

  it('409 면 트리를 새로 받게 한다 — 400 은 아니다 (대조군)', async () => {
    vi.mocked(revalidatePath).mockClear();
    putMock.mockRejectedValueOnce(
      new APIError(409, 'conflict', 'The resource was modified concurrently. Please retry.'),
    );
    const res = await setRateLimitAction({ target_id: 'a', scope: 'GLOBAL', rpm: 1 });
    expect(res.success).toBe(false);
    expect(revalidatePath).toHaveBeenCalledWith('/rate-limits');

    vi.mocked(revalidatePath).mockClear();
    putMock.mockRejectedValueOnce(new APIError(400, 'validation_error', 'bad'));
    await setRateLimitAction({ target_id: 'a', scope: 'GLOBAL', rpm: 1 });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it('USER/TEAM 의 UUID 는 그대로 둔다(인코딩 무해 — 대조군)', async () => {
    await setRateLimitAction({
      target_id: '11111111-1111-1111-1111-111111111111',
      scope: 'USER',
      rpm: 10,
    });
    expect(putMock.mock.calls[0][0]).toBe('/admin/rate-limits/user/11111111-1111-1111-1111-111111111111');
  });
});

describe('deleteUserRateLimitAction', () => {
  it('USER 오버라이드 제거 엔드포인트를 호출한다', async () => {
    const res = await deleteUserRateLimitAction('11111111-1111-1111-1111-111111111111');

    expect(res.success).toBe(true);
    expect(deleteMock).toHaveBeenCalledWith('/admin/rate-limits/user/11111111-1111-1111-1111-111111111111');
  });

  it('빈 id 는 요청하지 않는다', async () => {
    const res = await deleteUserRateLimitAction('');
    expect(res.success).toBe(false);
    expect(deleteMock).not.toHaveBeenCalled();
  });
});

describe('fetchRateLimitUsage', () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubBody(body: unknown) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ json: async () => body }));
  }

  it('measured / limit_rpm 을 그대로 전달한다', async () => {
    stubBody({ available: true, measured: true, limit_rpm: 60, window_sec: 60, rpm_used_total: 3, by_model: [] });
    const u = await fetchRateLimitUsage('USER', 'u1');
    expect(u).toMatchObject({ available: true, measured: true, limit_rpm: 60, rpm_used_total: 3 });
  });

  it('measured 가 없는 응답(구버전 API·프록시 오류 본문)은 측정 불가로 떨어진다', async () => {
    // 예전 응답 모양 — 이걸 measured=true 로 읽으면 "요청 없음" 오표시가 되살아난다.
    stubBody({ available: true, window_sec: 60, rpm_used_total: 0, by_model: [] });
    const u = await fetchRateLimitUsage('USER', 'u1');
    expect(u.measured).toBe(false);
    expect(u.limit_rpm).toBeNull();
  });

  it('네트워크 실패는 available=false, measured=false', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    const u = await fetchRateLimitUsage('GLOBAL', 'claude-opus-4-7');
    expect(u).toMatchObject({ available: false, measured: false, limit_rpm: null });
  });
});
