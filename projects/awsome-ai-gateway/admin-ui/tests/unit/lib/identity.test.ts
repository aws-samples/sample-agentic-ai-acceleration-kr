// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * fetchIdentity(lib/identity.ts) — admin-api `GET /admin/me` 의 답을 세 갈래로 나눈다.
 *
 * 갈래가 콜백의 결정을 그대로 정한다: ok 는 역할 쿠키를 굽고, denied 는 세션을 만들지 않고, unavailable
 * 은 토큰이 역할을 줄 때만 들여보낸다. 그래서 "판정"(401/403)과 "판정 못 받음"(그 밖의 전부)이
 * 섞이면 안 된다 — 404(옛 admin-api)를 denied 로 보면 관리자 그룹 구성원까지 잠긴다.
 */

// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchIdentity } from '@/lib/identity';

// ⚠️ ADMIN_API_URL 은 모듈 로드 시점에 const 로 굳는다(api-client.ts 와 같다) — 기본값을 쓴다.
const ADMIN_ME_URL = 'http://admin-api:8080/admin/me';
const TOKEN = 'header.eyJzdWIiOiJ1In0.sig';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** admin-api 의 HTTPException 봉투(admin-api/src/app/main.py `_http_exception_envelope`). */
function envelope(status: number, type: string, message: string): Response {
  return json(status, { error: { type, code: type.toUpperCase(), message } });
}

function stubFetch(impl: (url: string, init: RequestInit) => Promise<Response> | Response) {
  const spy = vi.fn(async (url: string | URL, init?: RequestInit) => impl(String(url), init ?? {}));
  vi.stubGlobal('fetch', spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fetchIdentity — 요청', () => {
  it('GET /admin/me 에 Bearer 토큰 · no-store · 시간 제한 신호를 싣는다', async () => {
    const spy = stubFetch(() =>
      json(200, { user_id: 'u-1', email: 'a@example.com', role: 'ADMIN', team_id: null }),
    );

    await fetchIdentity(TOKEN);

    expect(spy).toHaveBeenCalledTimes(1);
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe(ADMIN_ME_URL);
    expect(init?.method ?? 'GET').toBe('GET');
    expect(new Headers(init?.headers).get('authorization')).toBe(`Bearer ${TOKEN}`);
    expect(init?.cache).toBe('no-store');
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it('시간 제한은 5초다', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    stubFetch(() => json(200, { role: 'ADMIN' }));
    await fetchIdentity(TOKEN);
    expect(timeout).toHaveBeenCalledWith(5000);
  });
});

describe('fetchIdentity — ok', () => {
  it.each(['ADMIN', 'TEAM_LEADER', 'DEVELOPER'])('200 + %s', async (role) => {
    stubFetch(() =>
      json(200, { user_id: 'u-1', email: 'lead@example.com', role, team_id: 'team-1' }),
    );
    expect(await fetchIdentity(TOKEN)).toEqual({
      kind: 'ok',
      role,
      email: 'lead@example.com',
      userId: 'u-1',
      teamId: 'team-1',
    });
  });

  it('team_id 가 null 이면 teamId 도 null', async () => {
    stubFetch(() => json(200, { user_id: 'u-1', email: 'a@example.com', role: 'ADMIN', team_id: null }));
    expect(await fetchIdentity(TOKEN)).toMatchObject({ kind: 'ok', teamId: null });
  });
});

describe('fetchIdentity — denied (admin-api 의 판정)', () => {
  it('401 → denied 401, code 는 error.message', async () => {
    stubFetch(() => envelope(401, 'unauthorized', 'Invalid or expired token'));
    expect(await fetchIdentity(TOKEN)).toEqual({
      kind: 'denied',
      status: 401,
      code: 'Invalid or expired token',
    });
  });

  it.each(['user_not_provisioned', 'user_deactivated', 'required_group_missing'])(
    '403 %s → denied 403',
    async (code) => {
      stubFetch(() => envelope(403, 'forbidden', code));
      expect(await fetchIdentity(TOKEN)).toEqual({ kind: 'denied', status: 403, code });
    },
  );

  it('403 이 FastAPI 원형 {"detail": "..."} 이어도 code 를 읽는다', async () => {
    stubFetch(() => json(403, { detail: 'user_not_provisioned' }));
    expect(await fetchIdentity(TOKEN)).toEqual({
      kind: 'denied',
      status: 403,
      code: 'user_not_provisioned',
    });
  });

  it('403 본문이 JSON 이 아니면 code 는 빈 문자열', async () => {
    stubFetch(() => new Response('<html>Forbidden</html>', { status: 403 }));
    expect(await fetchIdentity(TOKEN)).toEqual({ kind: 'denied', status: 403, code: '' });
  });

  it('403 의 detail 이 문자열이 아니면 code 는 빈 문자열', async () => {
    stubFetch(() => json(403, { detail: [{ loc: ['header'], msg: 'x' }] }));
    expect(await fetchIdentity(TOKEN)).toEqual({ kind: 'denied', status: 403, code: '' });
  });
});

describe('fetchIdentity — unavailable (판정을 못 받음)', () => {
  it('404 — /admin/me 가 없는 옛 admin-api', async () => {
    stubFetch(() => json(404, { detail: 'Not Found' }));
    const r = await fetchIdentity(TOKEN);
    expect(r.kind).toBe('unavailable');
    expect(r).toMatchObject({ reason: expect.stringContaining('404') });
  });

  it.each([500, 502])('%i', async (status) => {
    stubFetch(() => new Response('upstream error', { status }));
    expect(await fetchIdentity(TOKEN)).toMatchObject({
      kind: 'unavailable',
      reason: expect.stringContaining(String(status)),
    });
  });

  it('503 — IdP 에 닿지 못한 admin-api', async () => {
    stubFetch(() => envelope(503, 'service_unavailable', 'Identity provider is unreachable'));
    expect(await fetchIdentity(TOKEN)).toMatchObject({
      kind: 'unavailable',
      reason: expect.stringContaining('503'),
    });
  });

  it('연결 실패(fetch 가 reject)', async () => {
    stubFetch(() => Promise.reject(new TypeError('fetch failed')));
    expect(await fetchIdentity(TOKEN)).toMatchObject({
      kind: 'unavailable',
      reason: expect.stringContaining('fetch failed'),
    });
  });

  it('fetch 가 동기로 던져도', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        throw new Error('sync boom');
      }),
    );
    expect(await fetchIdentity(TOKEN)).toMatchObject({ kind: 'unavailable' });
  });

  it('시간 초과 — 신호가 끊기면 기다리지 않고 unavailable', async () => {
    // 실제 5초를 기다리지 않는다: AbortSignal.timeout 이 돌려줄 신호를 우리가 쥐고 직접 끊는다.
    const controller = new AbortController();
    vi.spyOn(AbortSignal, 'timeout').mockReturnValue(controller.signal);
    const spy = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(init.signal?.reason));
        }),
    );

    const pending = fetchIdentity(TOKEN);
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy.mock.calls[0][1]?.signal).toBe(controller.signal);
    controller.abort(new DOMException('The operation was aborted due to timeout', 'TimeoutError'));

    expect(await pending).toMatchObject({
      kind: 'unavailable',
      reason: expect.stringContaining('5000'),
    });
  });

  it('다른 이유로 끊긴 요청(AbortError)도 unavailable', async () => {
    stubFetch(() => Promise.reject(new DOMException('This operation was aborted', 'AbortError')));
    expect(await fetchIdentity(TOKEN)).toMatchObject({ kind: 'unavailable' });
  });

  it('200 인데 본문이 JSON 이 아님', async () => {
    stubFetch(() => new Response('<html>login</html>', { status: 200 }));
    expect(await fetchIdentity(TOKEN)).toMatchObject({
      kind: 'unavailable',
      reason: expect.stringContaining('JSON'),
    });
  });

  it.each([
    ['모르는 역할', { role: 'SUPERUSER' }],
    ['소문자 역할', { role: 'admin' }],
    ['역할 없음', { user_id: 'u-1', email: 'a@example.com' }],
    ['숫자 역할', { role: 1 }],
    ['null 본문', null],
    ['배열 본문', ['ADMIN']],
  ])('200 인데 %s', async (_label, body) => {
    stubFetch(() => json(200, body));
    expect(await fetchIdentity(TOKEN)).toMatchObject({ kind: 'unavailable' });
  });
});
