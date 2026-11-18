// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * middleware 가 역할 쿠키(`admin_role`)로 화면을 연다 — 그리고 그 쿠키의 수명은 토큰과 같다.
 *
 * 결함: IdP 토큰에는 role 클레임이 없어 middleware 는 관리자 그룹이 아닌 사람을 전부 /403 으로 보냈다.
 * admin-api 는 같은 사람을 이메일·DB 역할로 인가하고 있었다(API 는 되는데 화면은 막힘).
 *
 * 여기서 고정하는 것:
 *   1. 역할 쿠키가 주는 역할대로 PAGE_PERMISSIONS 가 적용된다(넓히지도 좁히지도 않는다).
 *   2. 다른 토큰에 묶인 쿠키는 무시된다. 토큰이 역할을 주면 쿠키보다 먼저다.
 *   3. 토큰을 버릴 때(만료·손상·로그아웃·dev 로그인) 역할 쿠키도 같이 버린다.
 */

// @vitest-environment node

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';

import { POST as devLoginPOST } from '@/app/api/auth/dev-login/route';
import { POST as logoutPOST } from '@/app/api/auth/logout/route';
import { encodeRoleCookie } from '@/lib/roleCookie';
import { middleware } from '@/middleware';

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

function token(payload: Record<string, unknown>): string {
  return `header.${b64url(payload)}.sig`;
}

const inAnHour = () => Math.floor(Date.now() / 1000) + 3600;

/** 관리자 그룹이 아닌 IdP 토큰 — 토큰만으로는 역할이 없다. */
const IDP = token({ sub: 'idp-subject-1', email: 'person@example.com', groups: ['Developers'], exp: inAnHour() });
const OTHER_IDP = token({ sub: 'idp-subject-2', email: 'other@example.com', groups: ['Developers'], exp: inAnHour() });

function pageRequest(path: string, cookies: Record<string, string>): NextRequest {
  const r = new NextRequest(`http://admin.test${path}`);
  for (const [k, v] of Object.entries(cookies)) r.cookies.set(k, v);
  return r;
}

async function asRole(role: 'ADMIN' | 'TEAM_LEADER' | 'DEVELOPER', path: string): Promise<Response> {
  return middleware(pageRequest(path, { admin_jwt: IDP, admin_role: await encodeRoleCookie(role, IDP) }));
}

function expectPasses(res: Response): void {
  expect(res.status).toBe(200);
  expect(res.headers.get('location')).toBeNull();
}

function expectRedirect(res: Response, path: string): void {
  expect(res.status).toBe(307);
  const url = new URL(res.headers.get('location')!);
  expect(url.origin).toBe('http://admin.test');
  expect(url.pathname).toBe(path);
}

function setCookies(res: Response): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of res.headers.getSetCookie()) out[c.split('=')[0]] = c;
  return out;
}

const CLEARED = /Max-Age=0|Expires=Thu, 01 Jan 1970/;

beforeEach(() => {
  // 관리자 그룹은 있지만 IDP 토큰은 그 그룹이 아니다 — "그룹으로는 ADMIN 아님" 을 분명히 한다.
  vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// ───────────────────────── 1. 역할별 화면 ─────────────────────────

describe('middleware — 역할 쿠키가 없으면 오늘과 같다', () => {
  it('관리자 그룹이 아닌 IdP 토큰만 있으면 / 는 /403', async () => {
    expectRedirect(await middleware(pageRequest('/', { admin_jwt: IDP })), '/403');
  });
});

describe('middleware — TEAM_LEADER', () => {
  it.each(['/', '/budgets', '/my'])('%s 는 열린다', async (path) => {
    expectPasses(await asRole('TEAM_LEADER', path));
  });

  it.each(['/keys', '/users', '/models', '/chat'])('%s 는 /403', async (path) => {
    expectRedirect(await asRole('TEAM_LEADER', path), '/403');
  });
});

describe('middleware — DEVELOPER', () => {
  it('/my 는 열린다', async () => {
    expectPasses(await asRole('DEVELOPER', '/my'));
  });

  it.each(['/', '/budgets', '/keys'])('%s 는 /403', async (path) => {
    expectRedirect(await asRole('DEVELOPER', path), '/403');
  });
});

describe('middleware — ADMIN (이메일로만 등록된 관리자)', () => {
  it.each(['/', '/keys', '/budgets', '/models', '/apps', '/rate-limits', '/users', '/monitoring', '/chat'])(
    '%s 는 열린다',
    async (path) => {
      expectPasses(await asRole('ADMIN', path));
    },
  );

  it('/my 는 여전히 /403 이다 (PAGE_PERMISSIONS 그대로)', async () => {
    expectRedirect(await asRole('ADMIN', '/my'), '/403');
  });
});

// ───────────────────────── 2. 묶임과 우선순위 ─────────────────────────

describe('middleware — 쿠키는 그 토큰에만 통한다', () => {
  it('다른 토큰에 묶인 ADMIN 쿠키는 무시된다', async () => {
    const res = await middleware(
      pageRequest('/', { admin_jwt: IDP, admin_role: await encodeRoleCookie('ADMIN', OTHER_IDP) }),
    );
    expectRedirect(res, '/403');
  });

  it('쓰레기 값은 무시된다 (500 아님)', async () => {
    expectRedirect(await middleware(pageRequest('/', { admin_jwt: IDP, admin_role: 'ADMIN' })), '/403');
  });

  it('role 클레임 토큰은 쿠키를 보지 않는다 — TEAM_LEADER 토큰 + ADMIN 쿠키 → /keys 는 /403', async () => {
    const leader = token({ sub: 'u', role: 'TEAM_LEADER', exp: inAnHour() });
    const res = await middleware(
      pageRequest('/keys', { admin_jwt: leader, admin_role: await encodeRoleCookie('ADMIN', leader) }),
    );
    expectRedirect(res, '/403');
  });

  it('관리자 그룹 토큰은 낮은 역할 쿠키로 강등되지 않는다', async () => {
    const groupAdmin = token({ sub: 'u', groups: ['GatewayAdmin'], exp: inAnHour() });
    const res = await middleware(
      pageRequest('/keys', { admin_jwt: groupAdmin, admin_role: await encodeRoleCookie('DEVELOPER', groupAdmin) }),
    );
    expectPasses(res);
  });
});

// ───────────────────────── 3. 같이 버린다 ─────────────────────────

describe('middleware — 토큰을 버릴 때 역할 쿠키도 버린다', () => {
  it('만료된 토큰 → 로그인으로, admin_jwt 와 admin_role 둘 다 지운다', async () => {
    const expired = token({ sub: 'idp-subject-1', groups: ['Developers'], exp: Math.floor(Date.now() / 1000) - 60 });
    const res = await middleware(
      pageRequest('/budgets', { admin_jwt: expired, admin_role: await encodeRoleCookie('TEAM_LEADER', expired) }),
    );

    expectRedirect(res, '/api/auth/login');
    const jar = setCookies(res);
    expect(jar['admin_jwt']).toMatch(CLEARED);
    expect(jar['admin_role']).toMatch(CLEARED);
    expect(jar['admin_role']).toMatch(/Path=\//);
  });

  it('손상된 토큰 → 로그인으로, 둘 다 지운다', async () => {
    const res = await middleware(pageRequest('/', { admin_jwt: 'not-a-jwt', admin_role: 'ADMIN.x' }));
    expectRedirect(res, '/api/auth/login');
    const jar = setCookies(res);
    expect(jar['admin_jwt']).toMatch(CLEARED);
    expect(jar['admin_role']).toMatch(CLEARED);
  });

  it('쿠키가 아예 없으면 아무것도 지우지 않는다 (오늘과 같다)', async () => {
    const res = await middleware(pageRequest('/', {}));
    expectRedirect(res, '/api/auth/login');
    expect(res.headers.getSetCookie()).toEqual([]);
  });
});

describe('logout — admin_role 도 지운다', () => {
  it.each([
    ['https', true],
    ['http', false],
  ])('%s', async (proto, secure) => {
    const res = await logoutPOST(
      new NextRequest('http://admin.test/api/auth/logout', {
        method: 'POST',
        headers: { 'x-forwarded-proto': proto },
      }),
    );

    expect(res.status).toBe(303);
    const jar = setCookies(res);
    for (const name of ['admin_jwt', 'admin_role']) {
      expect(jar[name], `${name} 가 지워지지 않았다`).toMatch(/Max-Age=0/);
      expect(jar[name]).toMatch(/HttpOnly/i);
      expect(jar[name]).toMatch(/SameSite=Lax/i);
      expect(jar[name]).toMatch(/Path=\//);
      if (secure) expect(jar[name]).toMatch(/Secure/i);
      else expect(jar[name]).not.toMatch(/Secure/i);
    }
  });
});

describe('dev-login POST — 앞 세션의 admin_role 을 지운다', () => {
  beforeEach(() => {
    vi.stubEnv('DEV_LOGIN_ENABLED', 'true');
    vi.stubEnv('APP_ENV', 'test');
  });

  function post(proto: string): Promise<Response> {
    return devLoginPOST(
      new NextRequest('http://admin.test/api/auth/dev-login', {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded', 'x-forwarded-proto': proto },
        body: 'role=TEAM_LEADER',
      }),
    );
  }

  it.each([
    ['https', true],
    ['http', false],
  ])('%s — admin_jwt 는 굽고 admin_role 은 같은 속성으로 지운다', async (proto, secure) => {
    const jar = setCookies(await post(proto));

    expect(jar['admin_jwt']).toMatch(/^admin_jwt=dev\./);
    expect(jar['admin_role']).toMatch(/^admin_role=;/);
    expect(jar['admin_role']).toMatch(/Max-Age=0/);
    for (const name of ['admin_jwt', 'admin_role']) {
      expect(jar[name]).toMatch(/HttpOnly/i);
      expect(jar[name]).toMatch(/SameSite=Lax/i);
      expect(jar[name]).toMatch(/Path=\//);
      if (secure) expect(jar[name]).toMatch(/Secure/i);
      else expect(jar[name]).not.toMatch(/Secure/i);
    }
  });

  it('꺼져 있으면 어떤 쿠키도 건드리지 않는다 (오늘과 같다)', async () => {
    vi.stubEnv('DEV_LOGIN_ENABLED', 'false');
    const res = await post('http');
    expect(res.status).toBe(503);
    expect(res.headers.getSetCookie()).toEqual([]);
  });
});
