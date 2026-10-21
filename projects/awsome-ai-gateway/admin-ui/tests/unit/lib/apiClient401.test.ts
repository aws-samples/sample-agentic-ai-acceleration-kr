// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 401(인증 만료·무효) 처리 — **실제 코드를 실행해서** 검증한다.
 *
 * 고친 결함: admin-ui 에는 실행되는 401 분기가 하나도 없었다.
 *   - src/lib/api-client.ts 의 분기는 `!response.ok` 와 `status === 204` 딱 둘이었다.
 *     401 과 403 이 같은 APIError 로 뭉개져서 호출부가 구분할 수단이 없었다.
 *   - `grep -rn "401" src/` 는 주석만 잡혔다(middleware.ts / lib/auth.ts / actions/users.ts).
 *   - middleware 는 이걸 못 막는다: 공개 경로 분기가 `/api/` 전체를 통과시키므로
 *     브라우저가 직접 때리는 프록시 라우트(src/app/api/**)에는 만료 검사가 없다.
 *     그래서 대시보드를 열어둔 채 필터만 바꾸면(문서 내비게이션이 없어 middleware 가
 *     아예 돌지 않는다) 카드에 "조회 실패: HTTP 401" 이 찍히고 낡은 숫자가 남았다.
 *
 * ⚠️ 이 파일의 핵심 대조군은 **403** 이다. 이 부류의 버그는 "ok 아니면 다 로그인으로"
 *    처럼 과잉 반응하는 쪽으로 잘못 고쳐지는데, 그러면 TEAM_LEADER 가 admin 전용
 *    엔드포인트에서 받는 정상 403 에도 로그인으로 튕겨 대시보드를 못 쓴다.
 *    403 은 반드시 예전 동작(APIError / '조회 실패' 문구)을 유지해야 한다.
 *
 * 실행 환경은 node 다(jsdom 아님) — next/server 의 라우트 핸들러를 그대로 import 해
 * 돌리기 위해서다. window 는
 * 리다이렉트 검증에 필요한 만큼만 스텁한다.
 */

// @vitest-environment node

import { describe, it, expect, vi, afterEach } from 'vitest';
import { NextRequest } from 'next/server';

import { adminAPI, UnauthorizedError, isUnauthorized } from '@/lib/api-client';
import { APIError } from '@/lib/utils/retry';
import {
  LOGIN_PATH,
  UNAUTHORIZED_ERROR_CODE,
  redirectToLoginIfUnauthorized,
  unauthorizedBody,
} from '@/lib/utils/unauthorized';

import { GET as modelShareGET } from '@/app/api/dashboard/model-share/route';
import { GET as clientShareGET } from '@/app/api/dashboard/client-share/route';
import { GET as teamsGET } from '@/app/api/teams-proxy/route';
import { GET as rlUsageGET } from '@/app/api/rate-limits/usage/route';
import { POST as chatPOST } from '@/app/api/chat-proxy/[...path]/route';

// 라우트 핸들러와 api-client 둘 다 next/headers 를 쓴다(전자는 .get, 후자는 .toString).
vi.mock('next/headers', () => ({
  cookies: () => ({
    toString: () => 'admin_jwt=stub-token',
    get: (name: string) => (name === 'admin_jwt' ? { value: 'stub-token' } : undefined),
  }),
}));

/** 상류(admin-api) 응답 흉내. json() 호출 횟수를 세서 204 경로의 공허성을 잡는다. */
function upstream(status: number, body?: unknown) {
  const stub = {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    body: null as ReadableStream<Uint8Array> | null,
    jsonCalls: 0,
    async json() {
      stub.jsonCalls += 1;
      if (body === undefined) throw new SyntaxError('Unexpected end of JSON input');
      return body;
    },
    async text() {
      return body === undefined ? '' : JSON.stringify(body);
    },
    async blob() {
      return new Blob(['payload']);
    },
  };
  return stub;
}

/** fetch 를 스텁하고, 호출 기록을 돌려준다. */
function stubFetch(res: ReturnType<typeof upstream>) {
  const spy = vi.fn(async (_url: string | URL, _init?: RequestInit) => res);
  vi.stubGlobal('fetch', spy);
  return spy;
}

/** window.location.assign 스텁 — node 환경이라 우리가 직접 심는다. */
function stubWindow() {
  const assign = vi.fn();
  vi.stubGlobal('window', { location: { assign } });
  return assign;
}

// ⚠️ ADMIN_API_URL 은 모듈 로드 시점에 const 로 굳는다(api-client.ts / 각 라우트).
//    테스트 안에서 process.env 를 바꿔도 반영되지 않으므로 기본값을 그대로 쓴다.
const UPSTREAM = 'http://admin-api:8080';

afterEach(() => {
  vi.unstubAllGlobals();
});

// ─────────────────────────────────────────────────────────────────────────────
// 0) 하네스 공허성 대조군 — 하네스가 실제로 코드를 통과시키는지
// ─────────────────────────────────────────────────────────────────────────────

describe('하네스 공허성 대조군', () => {
  it('200 이면 api-client 가 파싱된 데이터를 그대로 돌려준다(401 분기가 매 응답에 걸리지 않는다)', async () => {
    const spy = stubFetch(upstream(200, { period: '2026-09', total_cost_usd: 12.5 }));

    const data = await adminAPI.get<{ period: string; total_cost_usd: number }>(
      '/admin/dashboard/summary',
      { period: '2026-09' },
    );

    // 하네스가 조용히 아무것도 안 했으면 아래 세 단정 중 하나라도 반드시 깨진다.
    expect(spy).toHaveBeenCalledTimes(1);
    expect(String(spy.mock.calls[0][0])).toBe(
      `${UPSTREAM}/admin/dashboard/summary?period=2026-09`,
    );
    expect(data).toEqual({ period: '2026-09', total_cost_usd: 12.5 });
  });

  it('window 스텁이 실제로 assign 을 기록한다(리다이렉트 단정이 공허하지 않다)', () => {
    const assign = stubWindow();
    expect(redirectToLoginIfUnauthorized({ status: 401 })).toBe(true);
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it('라우트 핸들러 하네스가 상류 200 본문을 그대로 통과시킨다', async () => {
    stubFetch(upstream(200, { period: '2026-09', team_id: 'all', models: [] }));

    const res = await modelShareGET(
      new NextRequest('http://admin.test/api/dashboard/model-share?period=2026-09'),
    );

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ period: '2026-09', team_id: 'all', models: [] });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 1) api-client — 401 은 구분 가능한 타입으로 던진다
// ─────────────────────────────────────────────────────────────────────────────

describe('AdminAPIClient 401', () => {
  it('401 이면 UnauthorizedError 를 던지고 로그인 진입점을 실어 준다', async () => {
    stubFetch(upstream(401, { detail: 'Not authenticated' }));

    const err = await adminAPI
      .get('/admin/keys/count')
      .then(() => null)
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(UnauthorizedError);
    expect(isUnauthorized(err)).toBe(true);
    const unauth = err as UnauthorizedError;
    expect(unauth.status).toBe(401);
    expect(unauth.error_code).toBe(UNAUTHORIZED_ERROR_CODE);
    expect(unauth.name).toBe('UnauthorizedError');
    // 호출부가 경로를 하드코딩하지 않아도 되게 — 계약은 정확히 /api/auth/login 하나다.
    expect(unauth.loginUrl).toBe('/api/auth/login');
    expect(unauth.loginUrl).toBe(LOGIN_PATH);
    // 상류가 준 사람용 문구는 유지한다(디버깅 정보를 버리지 않는다).
    expect(unauth.message).toBe('Not authenticated');
  });

  it('UnauthorizedError 는 APIError 다 — 기존 호출부/withRetry 가 안 깨진다', async () => {
    // ⚠️ 이걸 별도 클래스로 떼면 lib/actions/*.ts 의 `err instanceof APIError` 분기가
    //    전부 빠져나가 사용자에겐 "예기치 않은 오류" 만 남는다. 상속이 계약이다.
    stubFetch(upstream(401, {}));

    const err = await adminAPI.get('/admin/keys/count').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(APIError);
    expect(err).toBeInstanceOf(Error);
  });

  it('본문이 없어도(비어 있는 401) UnauthorizedError 를 던진다', async () => {
    // 게이트웨이/프록시가 본문 없는 401 을 줄 수 있다 — JSON 파싱 실패로 분기를 놓치면
    // 그때만 예전 동작으로 조용히 퇴화한다.
    stubFetch(upstream(401));

    const err = await adminAPI.get('/admin/keys/count').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(UnauthorizedError);
    expect((err as UnauthorizedError).message).toBeTruthy();
  });

  it('⚠️ 403 은 401 경로를 타지 않는다 — TEAM_LEADER 는 로그인으로 튕기면 안 된다', async () => {
    // src/app/page.tsx 는 403 을 allSettled 로 받아 '—' 로 렌더한다. 그게 정상 동작이고,
    // 여기서 UnauthorizedError 가 나오면 권한 낮은 사용자가 대시보드를 아예 못 쓴다.
    stubFetch(upstream(403, { error_code: 'FORBIDDEN', message: '권한이 없습니다' }));

    const err = await adminAPI.get('/admin/keys/count').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(APIError);
    expect(err).not.toBeInstanceOf(UnauthorizedError);
    expect(isUnauthorized(err)).toBe(false);
    expect((err as APIError).status).toBe(403);
    expect((err as APIError).error_code).toBe('FORBIDDEN');
  });

  it('500 도 401 경로를 타지 않는다(분기가 status 하나에만 반응한다)', async () => {
    stubFetch(upstream(500, { message: 'boom' }));

    const err = await adminAPI.get('/admin/models').catch((e: unknown) => e);

    expect(err).not.toBeInstanceOf(UnauthorizedError);
    expect((err as APIError).status).toBe(500);
  });

  it('204 는 그대로 undefined — 본문을 파싱하지 않는다', async () => {
    const res = upstream(204);
    stubFetch(res);

    await expect(adminAPI.delete('/admin/keys/k-1')).resolves.toBeUndefined();
    // 204 에 json() 을 부르면 SyntaxError 로 터진다 — 분기를 추가하며 순서를 흐트리면
    // 여기서 잡힌다.
    expect(res.jsonCalls).toBe(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 2) 클라이언트 헬퍼 — 401 → /api/auth/login 이동
// ─────────────────────────────────────────────────────────────────────────────

describe('redirectToLoginIfUnauthorized', () => {
  it('401 이면 /api/auth/login 으로 이동시키고 true 를 돌려준다', () => {
    const assign = stubWindow();

    expect(redirectToLoginIfUnauthorized({ status: 401 })).toBe(true);

    expect(assign).toHaveBeenCalledTimes(1);
    // 진입점 계약: 쿼리 없이 정확히 이 경로. (다른 에이전트가 만드는 라우트다.)
    expect(assign).toHaveBeenCalledWith('/api/auth/login');
  });

  it('⚠️ 403 은 이동시키지 않는다 — 권한 부족은 로그인 문제가 아니다', () => {
    const assign = stubWindow();

    expect(redirectToLoginIfUnauthorized({ status: 403 })).toBe(false);

    expect(assign).not.toHaveBeenCalled();
  });

  it('200/204/500 도 이동시키지 않는다', () => {
    const assign = stubWindow();

    for (const status of [200, 204, 400, 404, 500, 503]) {
      expect(redirectToLoginIfUnauthorized({ status })).toBe(false);
    }
    expect(assign).not.toHaveBeenCalled();
  });

  it('window 가 없어도(서버 렌더) 던지지 않고 판정만 돌려준다', () => {
    // 실수로 서버 경로에서 불려도 크래시가 나면 안 된다. 판정(true)은 유지해야
    // 호출부가 정상 경로를 계속 타지 않는다.
    expect(typeof (globalThis as { window?: unknown }).window).toBe('undefined');
    expect(() => redirectToLoginIfUnauthorized({ status: 401 })).not.toThrow();
    expect(redirectToLoginIfUnauthorized({ status: 401 })).toBe(true);
  });

  it('응답이 null/undefined 여도 안전하다', () => {
    expect(redirectToLoginIfUnauthorized(null)).toBe(false);
    expect(redirectToLoginIfUnauthorized(undefined)).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 3) 프록시 라우트 핸들러 — 401 본문 형태
// ─────────────────────────────────────────────────────────────────────────────

describe('프록시 라우트 401 본문', () => {
  it('본문 계약: error_code / login_url', () => {
    expect(unauthorizedBody()).toMatchObject({
      error_code: 'UNAUTHORIZED',
      login_url: '/api/auth/login',
    });
    expect(typeof unauthorizedBody().error).toBe('string');
  });

  it('model-share: 401 을 기계 판독용 본문으로 내려보낸다', async () => {
    stubFetch(upstream(401, { detail: 'Not authenticated' }));

    const res = await modelShareGET(
      new NextRequest('http://admin.test/api/dashboard/model-share?period=2026-09&team_id=all'),
    );

    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ error_code: 'UNAUTHORIZED' });
  });

  it('⚠️ model-share: 403 은 예전 그대로 — error_code 를 붙이지 않는다', async () => {
    stubFetch(upstream(403, {}));

    const res = await modelShareGET(
      new NextRequest('http://admin.test/api/dashboard/model-share?period=2026-09'),
    );

    expect(res.status).toBe(403);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body.error_code).toBeUndefined();
    expect(body.error).toBe('모델별 비용 점유율 조회 실패');
  });

  it('client-share: 401 → error_code, 500 은 예전 그대로', async () => {
    stubFetch(upstream(401, {}));
    let res = await clientShareGET(
      new NextRequest('http://admin.test/api/dashboard/client-share?period=2026-09'),
    );
    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ error_code: 'UNAUTHORIZED' });

    stubFetch(upstream(500, {}));
    res = await clientShareGET(
      new NextRequest('http://admin.test/api/dashboard/client-share?period=2026-09'),
    );
    expect(res.status).toBe(500);
    expect(((await res.json()) as Record<string, unknown>).error_code).toBeUndefined();
  });

  it('teams-proxy: 401 → error_code', async () => {
    stubFetch(upstream(401, {}));

    const res = await teamsGET();

    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ error_code: 'UNAUTHORIZED' });
  });

  it('rate-limits/usage: 401 은 fail-soft 200 에서 빼내되 available:false 는 유지한다', async () => {
    // 이 라우트는 의도적으로 fail-soft 다. 401 만 예외로 상태코드를 올려야 폴링이
    // 조용히 영원히 실패하지 않는다. 단, 기존 소비자(lib/utils/rateLimitUsage.ts)는
    // res.ok 를 보지 않고 본문만 읽으므로 available 필드를 없애면 형태가 달라진다.
    stubFetch(upstream(401, {}));

    const res = await rlUsageGET(
      new NextRequest('http://admin.test/api/rate-limits/usage?scope=team&scope_id=t-1'),
    );

    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({
      error_code: 'UNAUTHORIZED',
      available: false,
    });
  });

  it('⚠️ rate-limits/usage: 403/5xx 는 fail-soft 200 을 그대로 유지한다', async () => {
    stubFetch(upstream(403, {}));
    let res = await rlUsageGET(
      new NextRequest('http://admin.test/api/rate-limits/usage?scope=team&scope_id=t-1'),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ available: false, reason: 'upstream 403' });

    stubFetch(upstream(503, {}));
    res = await rlUsageGET(
      new NextRequest('http://admin.test/api/rate-limits/usage?scope=team&scope_id=t-1'),
    );
    expect(res.status).toBe(200);
  });

  it('chat-proxy: 401 은 SSE pass-through 대신 JSON error_code 를 준다', async () => {
    // 상류 401 은 스트림이 아니므로 흘려보낼 이유가 없다. 그대로 흘리면 useChatStream 이
    // `HTTP 401` 문자열만 던지고 사용자는 이유 없이 멈춘 채팅을 본다.
    stubFetch(upstream(401, { detail: 'Not authenticated' }));

    const res = await chatPOST(
      new NextRequest('http://admin.test/api/chat-proxy/admin/chat/sessions/s-1/messages', {
        method: 'POST',
        body: JSON.stringify({ content: '안녕' }),
      }),
      { params: { path: ['admin', 'chat', 'sessions', 's-1', 'messages'] } },
    );

    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ error_code: 'UNAUTHORIZED' });
  });

  it('⚠️ chat-proxy: 200 스트림은 계속 pass-through 다', async () => {
    const enc = new TextEncoder();
    const res200 = upstream(200);
    res200.body = new ReadableStream({
      start(c) {
        c.enqueue(enc.encode('event: text\ndata: {"chunk":"안녕"}\n\n'));
        c.close();
      },
    });
    res200.headers.set('content-type', 'text/event-stream');
    stubFetch(res200);

    const res = await chatPOST(
      new NextRequest('http://admin.test/api/chat-proxy/admin/chat/sessions/s-1/messages', {
        method: 'POST',
        body: JSON.stringify({ content: '안녕' }),
      }),
      { params: { path: ['admin', 'chat', 'sessions', 's-1', 'messages'] } },
    );

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/event-stream');
    expect(await res.text()).toContain('"chunk":"안녕"');
  });
});
