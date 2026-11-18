// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * OIDC 콜백이 쿠키를 굽기 전에 admin-api `GET /admin/me` 로 실효 역할을 묻는다.
 *
 * 결함: 콘솔은 역할을 토큰에서만 읽었다(role 클레임 → 관리자 그룹 → 없음). admin-api 는 이메일과 DB
 * 역할(TEAM_LEADER 등)로도 인가하므로, 이메일로만 등록된 관리자와 모든 팀 리더는 API 는 되는데 모든
 * 화면이 /403 이었다.
 *
 * 여기서 고정하는 것:
 *   1. ok — admin_jwt 와 같은 속성·수명의 admin_role(역할과 팀)을 굽고, 그 값은 **그 토큰**으로만 읽힌다.
 *      첫 화면은 권한표로 고른다 — `/` 를 못 여는 역할(DEVELOPER)은 `/my`.
 *   2. denied(401/403) — 세션을 만들지 않고 **앞 세션의 admin_jwt·admin_role 도 지운** 403 페이지.
 *      다른 실패 페이지(위조된 콜백 URL 로도 닿는다)와 503 은 앞 세션을 건드리지 않는다.
 *   3. unavailable — 토큰이 역할을 주면 오늘처럼 들여보내고(역할 쿠키 없이), 못 주면 503 페이지.
 *   4. 순서 — 토큰 형식·만료 검사를 통과한 뒤에만 묻는다.
 *
 * token 엔드포인트와 /admin/me 는 URL 로 갈라 답하는 fetch 스텁 하나로 흉내 낸다.
 */

// @vitest-environment node

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';

import { GET as callbackGET } from '@/app/api/auth/callback/route';
import { readRoleCookie } from '@/lib/roleCookie';
import { middleware } from '@/middleware';

// ───────────────────────── helpers ─────────────────────────

const TOKEN_URL = 'https://idp.example.test/oauth2/token';
// ADMIN_API_URL 은 모듈 로드 시점에 굳는다(lib/identity.ts) — 기본값을 쓴다.
const ADMIN_ME_URL = 'http://admin-api:8080/admin/me';
const STATE = 'state-value-0123456789';
const VERIFIER = 'verifier-value-0123456789012345678901234';

const VARS = [
  'OIDC_CLIENT_ID',
  'OIDC_AUTHORIZE_URL',
  'OIDC_TOKEN_URL',
  'OIDC_CLIENT_SECRET',
  'OIDC_REDIRECT_URI',
  'OIDC_SCOPES',
  'OIDC_COOKIE_TOKEN',
  'DEV_LOGIN_ENABLED',
  'ADMIN_GROUPS',
  'OIDC_GROUPS_CLAIM',
] as const;

const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of VARS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
  process.env.OIDC_CLIENT_ID = 'admin-ui-client';
  process.env.OIDC_AUTHORIZE_URL = 'https://idp.example.test/oauth2/authorize';
  process.env.OIDC_TOKEN_URL = TOKEN_URL;
  process.env.ADMIN_GROUPS = 'GatewayAdmin';
});

afterEach(() => {
  for (const k of VARS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  vi.unstubAllGlobals();
});

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

/** IdP id_token 모양 — role 클레임이 **없다**. */
function idpToken(claims: Record<string, unknown> = {}): string {
  return `header.${b64url({
    sub: 'idp-subject-1',
    email: 'person@example.com',
    exp: Math.floor(Date.now() / 1000) + 3600,
    ...claims,
  })}.sig`;
}

/** 관리자 그룹도 role 클레임도 없는 토큰 — 토큰만으로는 역할이 없다. */
const NO_ROLE_TOKEN = idpToken({ groups: ['Developers'] });
/** 관리자 그룹 구성원 — 토큰만으로 ADMIN(ADMIN_GROUPS=GatewayAdmin). */
const ADMIN_GROUP_TOKEN = idpToken({ sub: 'idp-subject-2', groups: ['GatewayAdmin'] });
/** admin-api 의 team_id 는 UUID 다. */
const TEAM = '0b7c6f1e-3a52-4c1d-9e8f-2a4b6c8d0e1f';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function envelope(status: number, type: string, message: string): Response {
  return json(status, { error: { type, code: type.toUpperCase(), message } });
}

function identity(role: string, extra: Record<string, unknown> = {}): Response {
  return json(200, { user_id: 'u-1', email: 'person@example.com', role, team_id: null, ...extra });
}

/** token 엔드포인트는 `idToken` 을, /admin/me 는 `me()` 를 돌려준다. 그 밖의 URL 은 실패. */
function stubUpstreams(idToken: string, me: () => Response | Promise<Response>) {
  const urls: string[] = [];
  const meCalls: RequestInit[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      urls.push(u);
      if (u === TOKEN_URL) return json(200, { id_token: idToken, expires_in: 3600 });
      if (u === ADMIN_ME_URL) {
        meCalls.push(init ?? {});
        return me();
      }
      throw new Error(`unexpected fetch: ${u}`);
    }),
  );
  return { urls, meCalls };
}

/** 같은 브라우저에 남아 있는 앞 세션(다른 계정일 수도 있다). 콜백은 이 쿠키를 읽지 않는다. */
const OLDER_SESSION = {
  admin_jwt: idpToken({ sub: 'earlier-subject', email: 'earlier@example.com' }),
  admin_role: 'ADMIN.aaaaaaaaaaaaaaaaaaaaaa',
};

function callback(
  headers: Record<string, string> = {},
  browserCookies: Record<string, string> = {},
  query = `code=CODE&state=${STATE}`,
): Promise<Response> {
  const r = new NextRequest(`http://admin.test/api/auth/callback?${query}`, {
    headers: { host: 'admin.test', ...headers },
  });
  r.cookies.set('oidc_state', STATE);
  r.cookies.set('oidc_verifier', VERIFIER);
  for (const [k, v] of Object.entries(browserCookies)) r.cookies.set(k, v);
  return callbackGET(r);
}

function setCookies(res: Response): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of res.headers.getSetCookie()) {
    out[c.split('=')[0]] = c;
  }
  return out;
}

function cookieValue(setCookie: string): string {
  return decodeURIComponent(setCookie.slice(setCookie.indexOf('=') + 1).split(';')[0]);
}

function maxAge(setCookie: string): number {
  return Number(/Max-Age=(\d+)/i.exec(setCookie)![1]);
}

/** 콜백이 구운 쿠키를 그대로 들고 페이지를 요청한다. */
function pageRequest(path: string, jar: Record<string, string>): NextRequest {
  const r = new NextRequest(`http://admin.test${path}`);
  for (const name of ['admin_jwt', 'admin_role']) {
    if (jar[name]) r.cookies.set(name, cookieValue(jar[name]));
  }
  return r;
}

function expectTempCookiesCleared(jar: Record<string, string>): void {
  // 임시 쿠키는 실패 경로에서도 정리된다(failure() 가 하던 그대로).
  expect(jar['oidc_state']).toMatch(/Max-Age=0/i);
  expect(jar['oidc_verifier']).toMatch(/Max-Age=0/i);
}

/** admin_jwt·admin_role 을 굽지도 지우지도 않았다 — 앞 세션이 그대로 남는다. */
function expectSessionUntouched(res: Response): void {
  const jar = setCookies(res);
  expect(jar['admin_jwt']).toBeUndefined();
  expect(jar['admin_role']).toBeUndefined();
  expectTempCookiesCleared(jar);
}

/** admin_jwt·admin_role 을 구울 때와 같은 속성으로 지웠다(Max-Age=0). */
function expectSessionCleared(res: Response, secure = false): void {
  const jar = setCookies(res);
  for (const name of ['admin_jwt', 'admin_role']) {
    expect(jar[name], `${name} 를 지우지 않았다`).toBeDefined();
    expect(cookieValue(jar[name])).toBe('');
    expect(jar[name]).toMatch(/Max-Age=0/);
    expect(jar[name]).toMatch(/HttpOnly/i);
    expect(jar[name]).toMatch(/SameSite=Lax/i);
    expect(jar[name]).toMatch(/Path=\//);
    if (secure) expect(jar[name]).toMatch(/Secure/i);
    else expect(jar[name]).not.toMatch(/Secure/i);
  }
  expectTempCookiesCleared(jar);
}

// ───────────────────────── 1. ok ─────────────────────────

describe('callback — admin-api 가 알려 준 역할로 로그인한다', () => {
  it('팀 리더: / 로 보내고 admin_role 은 그 토큰으로 TEAM_LEADER 와 그 팀으로 읽힌다', async () => {
    const { urls, meCalls } = stubUpstreams(NO_ROLE_TOKEN, () =>
      identity('TEAM_LEADER', { team_id: TEAM }),
    );

    const res = await callback();

    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/');
    const jar = setCookies(res);
    expect(cookieValue(jar['admin_jwt'])).toBe(NO_ROLE_TOKEN);
    const roleValue = cookieValue(jar['admin_role']);
    expect(await readRoleCookie(roleValue, NO_ROLE_TOKEN)).toEqual({
      role: 'TEAM_LEADER',
      teamId: TEAM,
    });
    // 다른 토큰에는 붙지 않는다.
    expect(await readRoleCookie(roleValue, ADMIN_GROUP_TOKEN)).toBeUndefined();

    // 묻는 순서와 방법: token 교환 → 그 토큰을 Bearer 로 /admin/me.
    expect(urls).toEqual([TOKEN_URL, ADMIN_ME_URL]);
    expect(new Headers(meCalls[0].headers).get('authorization')).toBe(`Bearer ${NO_ROLE_TOKEN}`);
    expect(meCalls[0].cache).toBe('no-store');
  });

  it('admin_role 은 admin_jwt 와 같은 속성·같은 Max-Age 다', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => identity('TEAM_LEADER'));
    const jar = setCookies(await callback());

    for (const name of ['admin_jwt', 'admin_role']) {
      expect(jar[name]).toMatch(/HttpOnly/i);
      expect(jar[name]).toMatch(/SameSite=Lax/i);
      expect(jar[name]).toMatch(/Path=\//);
      expect(jar[name]).not.toMatch(/Secure/i); // HTTP 종단
    }
    expect(maxAge(jar['admin_role'])).toBe(maxAge(jar['admin_jwt']));
    expect(maxAge(jar['admin_jwt'])).toBeGreaterThan(3500); // 토큰 exp(+3600) 에서 뽑았다
  });

  it('https 종단이면 admin_role 에도 Secure 가 붙는다', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => identity('TEAM_LEADER'));
    const jar = setCookies(await callback({ 'x-forwarded-proto': 'https' }));
    expect(jar['admin_jwt']).toMatch(/Secure/i);
    expect(jar['admin_role']).toMatch(/Secure/i);
  });

  it('이메일로만 등록된 관리자 → admin_role=ADMIN', async () => {
    const emailAdmin = idpToken({ email: 'admin@example.com' }); // 그룹 없음
    stubUpstreams(emailAdmin, () => identity('ADMIN', { email: 'admin@example.com' }));

    const res = await callback();

    expect(res.status).toBe(303);
    const jar = setCookies(res);
    expect(await readRoleCookie(cookieValue(jar['admin_role']), emailAdmin)).toEqual({
      role: 'ADMIN',
      teamId: null,
    });
  });

  it('team_id 가 null 이면 팀 조각 없는 두 조각 쿠키다', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => identity('TEAM_LEADER', { team_id: null }));
    const value = cookieValue(setCookies(await callback())['admin_role']);
    expect(value.split('.')).toHaveLength(2);
    expect(await readRoleCookie(value, NO_ROLE_TOKEN)).toEqual({ role: 'TEAM_LEADER', teamId: null });
  });

  it('구운 쿠키로 middleware 가 그 역할의 화면만 연다 (콜백 → middleware 연결)', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => identity('TEAM_LEADER'));
    const leaderJar = setCookies(await callback());
    const budgets = await middleware(pageRequest('/budgets', leaderJar));
    expect(budgets.headers.get('location')).toBeNull();
    const keys = await middleware(pageRequest('/keys', leaderJar));
    expect(new URL(keys.headers.get('location')!).pathname).toBe('/403');

    const emailAdmin = idpToken({ email: 'admin@example.com' });
    stubUpstreams(emailAdmin, () => identity('ADMIN'));
    const adminJar = setCookies(await callback());
    const adminKeys = await middleware(pageRequest('/keys', adminJar));
    expect(adminKeys.headers.get('location')).toBeNull();
  });

  it('대조군: 역할 쿠키를 빼면 같은 토큰은 / 에서 /403 이다', async () => {
    // 위 단정이 "토큰만으로도 통과" 해서 공허하지 않다는 것을 보인다.
    stubUpstreams(NO_ROLE_TOKEN, () => identity('TEAM_LEADER'));
    const jar = setCookies(await callback());
    delete jar['admin_role'];
    const res = await middleware(pageRequest('/', jar));
    expect(new URL(res.headers.get('location')!).pathname).toBe('/403');
  });
});

// ───────────────────────── 2. denied ─────────────────────────

describe('callback — admin-api 가 거절하면 세션을 만들지 않는다', () => {
  it.each([
    [
      '401 (콘솔 client id 불일치)',
      () => envelope(401, 'unauthorized', 'Invalid or expired token'),
      'Admin API rejected the session token',
      ['adminApi.oidc.adminUiClientId', 'adminUi.env.OIDC_CLIENT_ID'],
    ],
    [
      '403 user_not_provisioned',
      () => envelope(403, 'forbidden', 'user_not_provisioned'),
      'This account is not registered in the gateway',
      ['gateway-cli login'],
    ],
    [
      '403 user_deactivated',
      () => envelope(403, 'forbidden', 'user_deactivated'),
      'This account is deactivated',
      ['IdP', 'Cognito'],
    ],
    [
      '403 required_group_missing',
      () => envelope(403, 'forbidden', 'required_group_missing'),
      'This account is not in the required group',
      ['adminApi.oidc.requiredGroup'],
    ],
    [
      '403 그 밖의 코드',
      () => envelope(403, 'forbidden', 'Admin role required'),
      'Admin API refused the sign-in',
      ['Admin role required'],
    ],
    [
      '403 FastAPI 원형 detail',
      () => json(403, { detail: 'user_deactivated' }),
      'This account is deactivated',
      [],
    ],
  ])('%s → 403 페이지, 앞 세션의 admin_jwt·admin_role 도 지운다', async (_label, me, title, hints) => {
    stubUpstreams(NO_ROLE_TOKEN, me);

    const res = await callback({}, OLDER_SESSION);

    expect(res.status).toBe(403);
    expect(res.headers.get('content-type')).toContain('text/html');
    const html = await res.text();
    expect(html).toContain(`<h1>${title}</h1>`);
    for (const hint of hints) expect(html).toContain(hint);
    expectSessionCleared(res);
  });

  it('https 종단이면 지우는 쿠키에도 Secure 가 붙는다 (구울 때와 같은 속성)', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => envelope(403, 'forbidden', 'user_deactivated'));
    const res = await callback({ 'x-forwarded-proto': 'https' }, OLDER_SESSION);
    expect(res.status).toBe(403);
    expectSessionCleared(res, true);
  });

  it('토큰이 스스로 ADMIN(관리자 그룹)이어도 거절이 이긴다', async () => {
    // admin-api 가 받지 않는 토큰으로 세션을 만들면 모든 호출이 401/403 인 화면만 남는다.
    stubUpstreams(ADMIN_GROUP_TOKEN, () => envelope(401, 'unauthorized', 'Invalid or expired token'));
    const res = await callback({}, OLDER_SESSION);
    expect(res.status).toBe(403);
    expectSessionCleared(res);
  });

  it('거절 코드는 escape 된다', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => envelope(403, 'forbidden', '<img src=x onerror=alert(1)>'));
    const html = await (await callback()).text();
    expect(html).not.toContain('<img src=x');
    expect(html).toContain('&lt;img src=x');
  });
});

describe('callback — 다른 실패 페이지와 503 은 앞 세션을 건드리지 않는다', () => {
  // 위조된 콜백 URL 로도 닿는 실패에서 세션을 지우면 남을 로그아웃시키는 수단이 된다.
  it('state 불일치', async () => {
    const { urls } = stubUpstreams(NO_ROLE_TOKEN, () => identity('ADMIN'));
    const res = await callback({}, OLDER_SESSION, 'code=CODE&state=ATTACKER');
    expect(res.status).toBe(400);
    expect(urls).toHaveLength(0);
    expectSessionUntouched(res);
  });

  it('provider error', async () => {
    const { urls } = stubUpstreams(NO_ROLE_TOKEN, () => identity('ADMIN'));
    const res = await callback({}, OLDER_SESSION, 'error=access_denied&error_description=cancelled');
    expect(res.status).toBe(400);
    expect(urls).toHaveLength(0);
    expectSessionUntouched(res);
  });

  it('token 교환 실패', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json(400, { error: 'invalid_grant' })),
    );
    const res = await callback({}, OLDER_SESSION);
    expect(res.status).toBe(502);
    expect(await res.text()).toContain('invalid_grant');
    expectSessionUntouched(res);
  });

  it('503 Could not confirm your role (일시 장애 — 계정 판정이 아니다)', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => envelope(503, 'service_unavailable', 'Identity provider is unreachable'));
    const res = await callback({}, OLDER_SESSION);
    expect(res.status).toBe(503);
    expect(await res.text()).toContain('<h1>Could not confirm your role</h1>');
    expectSessionUntouched(res);
  });
});

// ───────────────────────── 첫 화면 ─────────────────────────

describe('callback — 첫 화면은 권한표로 고른다', () => {
  const ROLE_CLAIM = (role: string) => idpToken({ sub: `claim-${role}`, role });

  const notFound = () => json(404, { detail: 'Not Found' });

  it.each([
    ['DEVELOPER (admin-api)', '/my', NO_ROLE_TOKEN, () => identity('DEVELOPER')],
    ['TEAM_LEADER (admin-api)', '/', NO_ROLE_TOKEN, () => identity('TEAM_LEADER')],
    ['ADMIN (admin-api)', '/', NO_ROLE_TOKEN, () => identity('ADMIN')],
    ['관리자 그룹 토큰 + 조회 불가', '/', ADMIN_GROUP_TOKEN, notFound],
    ['관리자 그룹 토큰 + admin-api DEVELOPER (토큰이 먼저)', '/', ADMIN_GROUP_TOKEN, () => identity('DEVELOPER')],
    ['role 클레임 DEVELOPER + 조회 불가', '/my', ROLE_CLAIM('DEVELOPER'), notFound],
    ['role 클레임 DEVELOPER + admin-api ADMIN (토큰이 먼저)', '/my', ROLE_CLAIM('DEVELOPER'), () => identity('ADMIN')],
    ['role 클레임 TEAM_LEADER + admin-api DEVELOPER (토큰이 먼저)', '/', ROLE_CLAIM('TEAM_LEADER'), () => identity('DEVELOPER')],
    ['role 클레임 ADMIN', '/', ROLE_CLAIM('ADMIN'), () => identity('ADMIN')],
  ])('%s → %s', async (_label, landing, token, me) => {
    stubUpstreams(token, me);
    const res = await callback();
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe(landing);
  });

  it.each(['DEVELOPER', 'TEAM_LEADER', 'ADMIN'])(
    '%s 가 도착하는 화면은 그 세션으로 middleware 가 연다',
    async (role) => {
      stubUpstreams(NO_ROLE_TOKEN, () => identity(role));
      const res = await callback();
      const jar = setCookies(res);

      const landing = await middleware(pageRequest(res.headers.get('location')!, jar));
      expect(landing.status).toBe(200);
      expect(landing.headers.get('location')).toBeNull();
    },
  );

  it('대조군: DEVELOPER 세션으로 `/` 는 /403 이다 (예전 도착 화면)', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => identity('DEVELOPER'));
    const jar = setCookies(await callback());
    const root = await middleware(pageRequest('/', jar));
    expect(new URL(root.headers.get('location')!).pathname).toBe('/403');
  });
});

// ───────────────────────── 3. unavailable ─────────────────────────

const UNAVAILABLE: Array<[string, () => Response | Promise<Response>]> = [
  ['404 (옛 admin-api)', () => json(404, { detail: 'Not Found' })],
  ['500', () => new Response('boom', { status: 500 })],
  ['503 (IdP 불통)', () => envelope(503, 'service_unavailable', 'Identity provider is unreachable')],
  ['연결 실패', () => Promise.reject(new TypeError('fetch failed'))],
  ['200 인데 JSON 아님', () => new Response('<html></html>', { status: 200 })],
  ['200 인데 모르는 역할', () => identity('SUPERUSER')],
];

describe('callback — admin-api 의 판정을 못 받았을 때', () => {
  it.each(UNAVAILABLE)('%s + 관리자 그룹 토큰 → 오늘처럼 로그인, admin_role 없음', async (_l, me) => {
    stubUpstreams(ADMIN_GROUP_TOKEN, me);

    const res = await callback();

    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/');
    const jar = setCookies(res);
    expect(cookieValue(jar['admin_jwt'])).toBe(ADMIN_GROUP_TOKEN);
    expect(jar['admin_role']).toBeUndefined();
    // 관리자 그룹 구성원은 토큰만으로 관리자 화면이 열린다(잠기지 않는다).
    const keys = await middleware(pageRequest('/keys', jar));
    expect(keys.headers.get('location')).toBeNull();
  });

  it('role 클레임이 있는 토큰도 오늘처럼 들여보낸다', async () => {
    const internal = idpToken({ role: 'TEAM_LEADER' });
    stubUpstreams(internal, () => json(404, { detail: 'Not Found' }));
    const res = await callback();
    expect(res.status).toBe(303);
    expect(setCookies(res)['admin_role']).toBeUndefined();
  });

  it.each(UNAVAILABLE)('%s + 역할 없는 토큰 → 503, 쿠키 없음', async (_l, me) => {
    stubUpstreams(NO_ROLE_TOKEN, me);

    const res = await callback();

    expect(res.status).toBe(503);
    const html = await res.text();
    expect(html).toContain('<h1>Could not confirm your role</h1>');
    expect(html).toContain('admin-api');
    expectSessionUntouched(res);
  });

  it('503 페이지에 원인이 적힌다', async () => {
    stubUpstreams(NO_ROLE_TOKEN, () => json(404, { detail: 'Not Found' }));
    expect(await (await callback()).text()).toContain('HTTP 404');
  });
});

// ───────────────────────── 4. 순서 ─────────────────────────

describe('callback — 토큰 검사를 통과한 뒤에만 admin-api 에 묻는다', () => {
  it('이미 만료된 토큰 → 502, /admin/me 를 부르지 않는다', async () => {
    const expired = idpToken({ exp: Math.floor(Date.now() / 1000) - 10 });
    const { meCalls } = stubUpstreams(expired, () => identity('ADMIN'));
    const res = await callback();
    expect(res.status).toBe(502);
    expect(meCalls).toHaveLength(0);
  });

  it('못 읽는 토큰 → 502, /admin/me 를 부르지 않는다', async () => {
    const { meCalls } = stubUpstreams('opaque-token', () => identity('ADMIN'));
    const res = await callback();
    expect(res.status).toBe(502);
    expect(meCalls).toHaveLength(0);
  });

  it('state 불일치 → 어떤 상류도 부르지 않는다', async () => {
    const { urls } = stubUpstreams(NO_ROLE_TOKEN, () => identity('ADMIN'));
    const r = new NextRequest(`http://admin.test/api/auth/callback?code=CODE&state=ATTACKER`);
    r.cookies.set('oidc_state', STATE);
    r.cookies.set('oidc_verifier', VERIFIER);
    const res = await callbackGET(r);
    expect(res.status).toBe(400);
    expect(urls).toHaveLength(0);
  });
});
