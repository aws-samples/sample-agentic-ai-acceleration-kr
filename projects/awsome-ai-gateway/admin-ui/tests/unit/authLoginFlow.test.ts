// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 로그인 진입점(/api/auth/login) · OIDC 콜백(/api/auth/callback) · dev-login 비활성 응답 ·
 * middleware 의 리다이렉트 목적지를 **핸들러를 실제로 호출해서** 검증한다.
 *
 * 회귀(A3): middleware 가 `/api/auth/dev-login` 으로 보냈고 그 라우트는 DEV_LOGIN_ENABLED
 * != "true" 이면 `new NextResponse(null, { status: 404 })` 였다. prod 는 그 값이 "false"
 * 이므로 브라우저는 307 → **본문 없는 404** 에서 끝났고, admin_jwt 쿠키를 세팅하는 코드는
 * 트리 전체에서 dev-login 한 곳뿐이었다 → prod 에 로그인 경로가 0개.
 *
 * ⚠️ 문자열 grep 이 아니라 동작을 본다. 특히 code_challenge 는 "존재한다"가 아니라
 *    **쿠키에 심긴 verifier 의 SHA-256** 인지 다시 계산해서 대조한다 — 그게 PKCE 의 전부다.
 *
 * ⚠️ node 환경이 필요하다: crypto.subtle / btoa / fetch 스텁을 쓴다(jsdom 기본값이면
 *    crypto.subtle 이 없다).
 */

// @vitest-environment node

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { NextURL } from 'next/dist/server/web/next-url';
import { NextRequest } from 'next/server';
import { GET as loginGET } from '@/app/api/auth/login/route';
import { GET as callbackGET } from '@/app/api/auth/callback/route';
import { GET as devLoginGET, POST as devLoginPOST } from '@/app/api/auth/dev-login/route';
import { middleware } from '@/middleware';

// ───────────────────────── helpers ─────────────────────────

const OIDC_VARS = [
  'OIDC_CLIENT_ID',
  'OIDC_AUTHORIZE_URL',
  'OIDC_TOKEN_URL',
  'OIDC_CLIENT_SECRET',
  'OIDC_REDIRECT_URI',
  'OIDC_SCOPES',
  'OIDC_COOKIE_TOKEN',
  'DEV_LOGIN_ENABLED',
] as const;

const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of OIDC_VARS) {
    savedEnv[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of OIDC_VARS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  vi.unstubAllGlobals();
});

/** 완전 설정된 OIDC — (1) 분기. */
function configureOidc(extra: Record<string, string> = {}): void {
  process.env.OIDC_CLIENT_ID = 'admin-ui-client';
  process.env.OIDC_AUTHORIZE_URL = 'https://idp.example.test/oauth2/authorize';
  process.env.OIDC_TOKEN_URL = 'https://idp.example.test/oauth2/token';
  for (const [k, v] of Object.entries(extra)) process.env[k] = v;
}

function req(
  url: string,
  opts: { headers?: Record<string, string>; cookies?: Record<string, string> } = {},
): NextRequest {
  const r = new NextRequest(url, { headers: opts.headers });
  for (const [k, v] of Object.entries(opts.cookies ?? {})) r.cookies.set(k, v);
  return r;
}

/** Set-Cookie 를 이름 → 전체 문자열 맵으로. NextResponse 는 여러 개를 한 헤더에 합친다. */
function setCookies(res: Response): Record<string, string> {
  const raw =
    typeof (res.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie === 'function'
      ? (res.headers as unknown as { getSetCookie: () => string[] }).getSetCookie()
      : (res.headers.get('set-cookie') ?? '').split(/,(?=[^;]+?=)/);
  const out: Record<string, string> = {};
  for (const c of raw) {
    const trimmed = c.trim();
    if (!trimmed) continue;
    out[trimmed.split('=')[0]] = trimmed;
  }
  return out;
}

function cookieValue(setCookie: string): string {
  return decodeURIComponent(setCookie.slice(setCookie.indexOf('=') + 1).split(';')[0]);
}

function b64url(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** 라우트와 독립적으로 다시 계산하는 S256 — 구현을 베끼지 않고 대조한다. */
async function s256(verifier: string): Promise<string> {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(d));
}

function jwtWithExp(expSeconds: number | null, extraClaims: Record<string, unknown> = {}): string {
  const payload: Record<string, unknown> = {
    sub: '11111111-1111-1111-1111-111111111111',
    email: 'sso@example.test',
    role: 'ADMIN',
    ...extraClaims,
  };
  if (expSeconds !== null) payload.exp = expSeconds;
  const b64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `header.${b64}.sig`;
}

/** token 엔드포인트 스텁. 호출 인자를 캡처해서 PKCE/Basic 을 검증한다. */
function stubTokenEndpoint(body: unknown, status = 200) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const spy = vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    } as unknown as Response;
  });
  vi.stubGlobal('fetch', spy);
  return calls;
}

// ───────────────────────── 0. vacuity control ─────────────────────────

/**
 * 같은 오리진 리다이렉트의 새 계약: Location 은 **상대 경로**다.
 *
 * 왜 절대 URL 이 아니어야 하나: 컨테이너 안에서 request.url 은 0.0.0.0 으로 풀리고,
 * Host 헤더도 CloudFront→ALB 구성에서는 ALB 의 DNS 이름이 들어온다. 절대 URL 을 만들면
 * 로그인 직후 브라우저가 내부 호스트로 이동해 CloudFront 오리진을 잃는다(내부 호스트에는
 * dev-login 이 열려 있을 수 있어 UX 문제로 끝나지 않는다). 상대 Location 은 브라우저가
 * **자신이 요청한 URL** 기준으로 해석하므로 서버가 자기 외부 주소를 알 필요가 없다.
 *
 * 그래서 scheme/host 가 **없다는 것 자체**를 단정한다 — 절대 URL 로 되돌아가면 실패한다.
 */
function expectRelativeRedirect(res: Response, path: string): void {
  const loc = res.headers.get('location');
  expect(loc).toBe(path);
  expect(loc).not.toMatch(/^https?:\/\//);
  // `//host` 는 스킴 상대 URL 이라 외부로 나간다 — "/" 로 시작한다는 검사만으론 부족하다.
  expect(loc!.startsWith('//')).toBe(false);
}

describe('vacuity control — 하네스가 정말로 핸들러를 실행하는가', () => {
  it('loginGET 은 실제 Response 를 돌려주고, env 를 읽어 출력이 달라진다', async () => {
    // 이 테스트가 없으면 아래 모든 단정이 "핸들러가 아무것도 안 해도 통과"할 수 있다.
    expect(typeof loginGET).toBe('function');

    process.env.DEV_LOGIN_ENABLED = 'true';
    const devRes = await loginGET(req('http://admin.test/api/auth/login'));
    expect(devRes).toBeInstanceOf(Response);
    const devLocation = devRes.headers.get('location');

    delete process.env.DEV_LOGIN_ENABLED;
    configureOidc();
    const oidcRes = await loginGET(req('http://admin.test/api/auth/login'));
    const oidcLocation = oidcRes.headers.get('location');

    // 같은 핸들러 · 같은 요청인데 env 만 바뀌어 목적지가 갈렸다 ⇒ 코드가 실제로 돌았다.
    expect(devLocation).toBeTruthy();
    expect(oidcLocation).toBeTruthy();
    expect(oidcLocation).not.toBe(devLocation);
    expect(oidcLocation).toContain('idp.example.test');
  });

  it('callbackGET · devLoginGET · middleware 도 Response 를 돌려준다', async () => {
    const cb = await callbackGET(req('http://admin.test/api/auth/callback'));
    expect(cb).toBeInstanceOf(Response);
    expect(typeof cb.status).toBe('number');

    const dl = await devLoginGET();
    expect(dl).toBeInstanceOf(Response);

    const mw = await middleware(req('http://admin.test/'));
    expect(mw).toBeInstanceOf(Response);
  });
});

// ───────────────────────── 1. /api/auth/login ─────────────────────────

describe('GET /api/auth/login — dev 는 오늘과 동일해야 한다', () => {
  it('DEV_LOGIN_ENABLED=true + OIDC 미설정 → dev 폼으로 넘긴다', async () => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await loginGET(req('http://admin.test/api/auth/login'));

    expect(res.status).toBe(307);
    expectRelativeRedirect(res, '/api/auth/dev-login');
    // ⚠️ dev 분기에서는 state/verifier 쿠키를 만들지 않는다(dev 흐름 오염 금지).
    expect(Object.keys(setCookies(res))).not.toContain('oidc_state');
  });

  it('dev 분기는 호스트를 아예 싣지 않는다 — 그래서 어떤 프록시 뒤에서도 오리진이 유지된다', async () => {
    // 예전 계약은 "Host 헤더의 호스트를 유지" 였다. 그건 CloudFront→ALB 에서 깨진다
    // (Host 가 ALB 이름이면 절대 URL 이 내부 호스트를 가리킨다). 상대 Location 은
    // 호스트를 싣지 않으므로 브라우저가 보고 있던 오리진이 그대로 남는다.
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await loginGET(req('http://admin.internal:3000/api/auth/login'));
    expectRelativeRedirect(res, '/api/auth/dev-login');
    expect(res.headers.get('location')).not.toContain('admin.internal');
  });
});

describe('GET /api/auth/login — OIDC 설정됨', () => {
  it('302 로 authorize URL 로 보내고 필수 파라미터를 모두 싣는다', async () => {
    configureOidc();
    const res = await loginGET(
      req('http://admin.test/api/auth/login', {
        headers: { host: 'admin.test', 'x-forwarded-proto': 'https' },
      }),
    );

    expect(res.status).toBe(302);
    const loc = new URL(res.headers.get('location')!);
    expect(loc.origin + loc.pathname).toBe('https://idp.example.test/oauth2/authorize');
    expect(loc.searchParams.get('response_type')).toBe('code');
    expect(loc.searchParams.get('client_id')).toBe('admin-ui-client');
    expect(loc.searchParams.get('redirect_uri')).toBe('https://admin.test/api/auth/callback');
    expect(loc.searchParams.get('scope')).toBe('openid email profile');
    expect(loc.searchParams.get('code_challenge_method')).toBe('S256');
    expect(loc.searchParams.get('state')).toBeTruthy();
    expect(loc.searchParams.get('code_challenge')).toBeTruthy();
    expect(res.headers.get('cache-control')).toBe('no-store');
  });

  it('state/verifier 를 httpOnly · SameSite=Lax · 짧은 TTL 쿠키로 심는다', async () => {
    configureOidc();
    const res = await loginGET(
      req('http://admin.test/api/auth/login', {
        headers: { host: 'admin.test', 'x-forwarded-proto': 'https' },
      }),
    );

    const jar = setCookies(res);
    for (const name of ['oidc_state', 'oidc_verifier']) {
      expect(jar[name], `${name} 쿠키 없음`).toBeTruthy();
      expect(jar[name]).toMatch(/HttpOnly/i);
      expect(jar[name]).toMatch(/SameSite=Lax/i);
      expect(jar[name]).toMatch(/Secure/i); // x-forwarded-proto=https
      // authorize 왕복만 버티면 된다 — 세션 수명이 아니다.
      const maxAge = Number(/Max-Age=(\d+)/i.exec(jar[name])![1]);
      expect(maxAge).toBeGreaterThan(0);
      expect(maxAge).toBeLessThanOrEqual(900);
    }

    // state 쿠키 값 == URL 의 state 파라미터. 다르면 콜백이 100% 실패한다.
    const loc = new URL(res.headers.get('location')!);
    expect(cookieValue(jar['oidc_state'])).toBe(loc.searchParams.get('state'));
  });

  it('code_challenge 가 정말로 verifier 쿠키의 SHA-256(S256)이다', async () => {
    configureOidc();
    const res = await loginGET(
      req('http://admin.test/api/auth/login', { headers: { host: 'admin.test' } }),
    );

    const verifier = cookieValue(setCookies(res)['oidc_verifier']);
    const challenge = new URL(res.headers.get('location')!).searchParams.get('code_challenge');

    expect(challenge).toBe(await s256(verifier));
    // plain 폴백이 섞이지 않았는지 — verifier 를 그대로 보내면 PKCE 가 무의미하다.
    expect(challenge).not.toBe(verifier);
    // RFC 7636: 43~128자 unreserved
    expect(verifier).toMatch(/^[A-Za-z0-9\-._~]{43,128}$/);
  });

  it('매 요청마다 state/verifier 가 새로 생성된다 (캐시/재사용 금지)', async () => {
    configureOidc();
    const a = await loginGET(req('http://admin.test/api/auth/login'));
    const b = await loginGET(req('http://admin.test/api/auth/login'));
    expect(cookieValue(setCookies(a)['oidc_state'])).not.toBe(
      cookieValue(setCookies(b)['oidc_state']),
    );
  });

  it('HTTP 종단이면 Secure 를 붙이지 않는다 (붙이면 쿠키 저장 실패 → state 불일치)', async () => {
    configureOidc();
    const res = await loginGET(
      req('http://admin.test/api/auth/login', { headers: { host: 'admin.test' } }),
    );
    expect(setCookies(res)['oidc_state']).not.toMatch(/Secure/i);
    expect(new URL(res.headers.get('location')!).searchParams.get('redirect_uri')).toBe(
      'http://admin.test/api/auth/callback',
    );
  });

  it('x-forwarded-proto 가 콤마 목록이어도 첫 토큰을 쓴다 (프록시 체인)', async () => {
    // "https,http" 를 그대로 비교하면 Secure 가 빠지고 redirect_uri 가 http 로 조립돼
    // IdP 의 정확일치 검사에서 튕긴다.
    configureOidc();
    const res = await loginGET(
      req('http://admin.test/api/auth/login', {
        headers: { host: 'admin.test', 'x-forwarded-proto': 'https,http' },
      }),
    );
    expect(new URL(res.headers.get('location')!).searchParams.get('redirect_uri')).toBe(
      'https://admin.test/api/auth/callback',
    );
    expect(setCookies(res)['oidc_state']).toMatch(/Secure/i);
  });

  it('OIDC_REDIRECT_URI / OIDC_SCOPES 명시값이 우선한다', async () => {
    configureOidc({
      OIDC_REDIRECT_URI: 'https://sso.example.test/api/auth/callback',
      OIDC_SCOPES: 'openid profile',
    });
    const res = await loginGET(req('http://admin.test/api/auth/login'));
    const p = new URL(res.headers.get('location')!).searchParams;
    expect(p.get('redirect_uri')).toBe('https://sso.example.test/api/auth/callback');
    expect(p.get('scope')).toBe('openid profile');
  });

  it('OIDC 가 켜져 있으면 DEV_LOGIN_ENABLED=true 여도 SSO 가 이긴다', async () => {
    configureOidc();
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await loginGET(req('http://admin.test/api/auth/login'));
    expect(res.headers.get('location')).toContain('idp.example.test');
  });
});

describe('GET /api/auth/login — 설정이 없을 때는 읽히는 503 (본문 없는 404 금지)', () => {
  it('둘 다 미설정 → 503 + 비어 있는 env 이름을 본문에 찍는다', async () => {
    const res = await loginGET(req('http://admin.test/api/auth/login'));

    // ⚠️ 이 두 줄이 A3 의 핵심 회귀 가드다.
    expect(res.status).not.toBe(404);
    expect(res.status).toBe(503);

    const body = await res.text();
    expect(body.length).toBeGreaterThan(0);
    expect(body).toContain('OIDC_CLIENT_ID');
    expect(body).toContain('OIDC_AUTHORIZE_URL');
    expect(body).toContain('OIDC_TOKEN_URL');
    expect(body).toContain('DEV_LOGIN_ENABLED');
    expect(res.headers.get('content-type')).toContain('text/html');
  });

  it('반쪽 설정 → 빠진 변수만 이름으로 찍는다 (dev 폼으로 조용히 흘리지 않는다)', async () => {
    process.env.OIDC_CLIENT_ID = 'admin-ui-client';
    process.env.OIDC_AUTHORIZE_URL = 'https://idp.example.test/oauth2/authorize';
    // OIDC_TOKEN_URL 누락
    const res = await loginGET(req('http://admin.test/api/auth/login'));

    expect(res.status).toBe(503);
    const body = await res.text();
    expect(body).toContain('OIDC_TOKEN_URL');
    expect(body).not.toContain('<code>OIDC_CLIENT_ID</code>');
  });

  it('반쪽 설정 + dev 켜짐 → 503 이지만 dev 탈출구를 안내한다', async () => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    process.env.OIDC_CLIENT_ID = 'admin-ui-client';
    const res = await loginGET(req('http://admin.test/api/auth/login'));
    expect(res.status).toBe(503);
    expect(await res.text()).toContain('/api/auth/dev-login');
  });

  it('OIDC_AUTHORIZE_URL 이 URL 이 아니면 500 이 아니라 503', async () => {
    configureOidc({ OIDC_AUTHORIZE_URL: 'not a url' });
    const res = await loginGET(req('http://admin.test/api/auth/login'));
    expect(res.status).toBe(503);
    expect(await res.text()).toContain('OIDC_AUTHORIZE_URL');
  });
});

// ───────────────────────── 2. /api/auth/callback ─────────────────────────

describe('GET /api/auth/callback — state 검사(보안 핵심)', () => {
  it('state 불일치 → 거절하고 admin_jwt 를 절대 세팅하지 않는다', async () => {
    configureOidc();
    const calls = stubTokenEndpoint({ id_token: jwtWithExp(null) });

    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=abc&state=ATTACKER', {
        cookies: { oidc_state: 'VICTIM', oidc_verifier: 'v'.repeat(43) },
      }),
    );

    expect(res.status).toBe(400);
    const jar = setCookies(res);
    expect(jar['admin_jwt']).toBeUndefined();
    // 토큰 교환 자체를 시도해서도 안 된다 — 공격자의 code 를 IdP 에 태우면 안 된다.
    expect(calls.length).toBe(0);
    // 임시 쿠키는 정리한다(재사용 방지).
    expect(jar['oidc_state']).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
    expect(jar['oidc_verifier']).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
    expect((await res.text()).length).toBeGreaterThan(0);
  });

  it('state 쿠키가 없음(만료) → 거절, admin_jwt 없음', async () => {
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(null) });
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=abc&state=S', {
        cookies: { oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(400);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });

  it('state 쿼리 파라미터가 아예 없음 → 거절, admin_jwt 없음', async () => {
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(null) });
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=abc', {
        cookies: { oidc_state: 'S', oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(400);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });

  it('state 는 맞지만 verifier 쿠키가 없으면 거절 (PKCE 없는 교환 금지)', async () => {
    configureOidc();
    const calls = stubTokenEndpoint({ id_token: jwtWithExp(null) });
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=abc&state=S', {
        cookies: { oidc_state: 'S' },
      }),
    );
    expect(res.status).toBe(400);
    expect(calls.length).toBe(0);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });
});

describe('GET /api/auth/callback — happy path', () => {
  const STATE = 'state-value-0123456789';
  const VERIFIER = 'verifier-value-0123456789012345678901234';

  function callbackReq(query = `code=CODE&state=${STATE}`, headers: Record<string, string> = {}) {
    return req(`http://admin.test/api/auth/callback?${query}`, {
      headers: { host: 'admin.test', ...headers },
      cookies: { oidc_state: STATE, oidc_verifier: VERIFIER },
    });
  }

  it('admin_jwt httpOnly 쿠키를 세우고 / 로 보낸다', async () => {
    configureOidc();
    const exp = Math.floor(Date.now() / 1000) + 3600;
    const idToken = jwtWithExp(exp);
    stubTokenEndpoint({ id_token: idToken, access_token: 'AT', expires_in: 999 });

    const res = await callbackGET(callbackReq());

    expect(res.status).toBe(303);
    expectRelativeRedirect(res, '/');
    const jar = setCookies(res);
    expect(jar['admin_jwt']).toBeTruthy();
    expect(cookieValue(jar['admin_jwt'])).toBe(idToken);
    expect(jar['admin_jwt']).toMatch(/HttpOnly/i);
    expect(jar['admin_jwt']).toMatch(/SameSite=Lax/i);
    // 임시 쿠키는 정리된다.
    expect(jar['oidc_state']).toMatch(/Max-Age=0/i);
    expect(jar['oidc_verifier']).toMatch(/Max-Age=0/i);
  });

  it('token 요청에 code_verifier / code / redirect_uri / client_id 를 싣는다 (PKCE 실사용)', async () => {
    configureOidc();
    const calls = stubTokenEndpoint({ id_token: jwtWithExp(null) });

    await callbackGET(callbackReq());

    expect(calls.length).toBe(1);
    expect(calls[0].url).toBe('https://idp.example.test/oauth2/token');
    expect(calls[0].init.method).toBe('POST');
    const sent = new URLSearchParams(String(calls[0].init.body));
    expect(sent.get('grant_type')).toBe('authorization_code');
    expect(sent.get('code')).toBe('CODE');
    expect(sent.get('code_verifier')).toBe(VERIFIER);
    expect(sent.get('client_id')).toBe('admin-ui-client');
    expect(sent.get('redirect_uri')).toBe('http://admin.test/api/auth/callback');
  });

  it('public client — Authorization 헤더를 붙이지 않는다', async () => {
    configureOidc();
    const calls = stubTokenEndpoint({ id_token: jwtWithExp(null) });
    await callbackGET(callbackReq());
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(headers['Content-Type']).toBe('application/x-www-form-urlencoded');
  });

  it('confidential client — HTTP Basic 으로 secret 을 보낸다', async () => {
    configureOidc({ OIDC_CLIENT_SECRET: 's3cr3t' });
    const calls = stubTokenEndpoint({ id_token: jwtWithExp(null) });
    await callbackGET(callbackReq());
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Basic ${btoa('admin-ui-client:s3cr3t')}`);
    // secret 이 body 로 새 나가지 않는다.
    expect(String(calls[0].init.body)).not.toContain('s3cr3t');
  });

  it('쿠키 Max-Age 를 토큰 자신의 exp 에서 뽑는다 (하드코딩 24h 금지)', async () => {
    configureOidc();
    // expires_in 은 일부러 크게 준다 — exp 가 이겨야 한다.
    stubTokenEndpoint({
      id_token: jwtWithExp(Math.floor(Date.now() / 1000) + 1800),
      expires_in: 86400,
    });

    const res = await callbackGET(callbackReq());
    const maxAge = Number(/Max-Age=(\d+)/i.exec(setCookies(res)['admin_jwt'])![1]);
    expect(maxAge).toBeGreaterThan(1700);
    expect(maxAge).toBeLessThanOrEqual(1800);
  });

  it('exp 가 없으면 expires_in 으로 폴백한다', async () => {
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(null), expires_in: 600 });
    const res = await callbackGET(callbackReq());
    expect(Number(/Max-Age=(\d+)/i.exec(setCookies(res)['admin_jwt'])![1])).toBe(600);
  });

  it('exp/expires_in 둘 다 없으면 1시간 폴백 (무기한 금지)', async () => {
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(null) });
    const res = await callbackGET(callbackReq());
    expect(Number(/Max-Age=(\d+)/i.exec(setCookies(res)['admin_jwt'])![1])).toBe(3600);
  });

  it('이미 만료된 토큰은 쿠키에 넣지 않고 읽히는 실패로 끝낸다', async () => {
    // ⚠️ 예전 이 테스트는 "Max-Age 가 0/음수가 아니면 무한 리다이렉트 방지" 라고 주장했다.
    //    거짓 전제다 — middleware 의 판정 근거는 Max-Age 가 아니라 토큰의 exp 다
    //    (src/lib/auth.ts isSessionExpired, 30초 skew). 만료된 토큰을 굽으면 Max-Age 를
    //    아무리 크게 줘도 다음 요청에서 쿠키가 지워지고 로그인으로 되돌아와, IdP 세션이
    //    살아 있는 동안 **영원히 반복**된다. 그래서 굽지 않는 것이 유일한 해결이다.
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(Math.floor(Date.now() / 1000) - 10) });
    const res = await callbackGET(callbackReq());

    expect(res.status).toBe(502);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
    const html = await res.text();
    expect(html).toContain('exp');
    expect(html).toMatch(/무한/);
  });

  it('수명이 짧지만 유효한 토큰은 Max-Age 하한으로 올려서 굽는다', async () => {
    // MIN_COOKIE_MAX_AGE 의 진짜 용도 — 시계 오차로 Max-Age 가 0 이 되어 Set-Cookie 가
    // 즉시 삭제 지시가 되는 것을 막는다(만료 토큰 방어와는 다른 문제다).
    //
    // ⚠️ exp 는 **30초 skew 밖**이어야 한다. isSessionExpired(src/lib/auth.ts)가 30초를
    //    미리 빼고 보므로 `now+5` 는 이미 만료로 판정돼 위 502 경로를 탄다(실제로 그렇게
    //    한 번 틀렸다). 45초면 skew(30) 는 넘고 하한(60) 아래라 클램프만 검증된다.
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(Math.floor(Date.now() / 1000) + 45) });
    const res = await callbackGET(callbackReq());

    expect(res.status).toBe(303);
    expect(Number(/Max-Age=(\d+)/i.exec(setCookies(res)['admin_jwt'])![1])).toBeGreaterThanOrEqual(60);
  });

  it('admin-ui 가 못 읽는 불투명 토큰은 쿠키에 넣지 않는다 (무한 리다이렉트 차단)', async () => {
    // 실제 재현 경로: OIDC_COOKIE_TOKEN=access_token + access_token 이 불투명한 IdP
    // (Okta org 서버). 예전엔 그 문자열을 그대로 admin_jwt 로 구웠고, middleware 의
    // parseJWT 가 throw → 쿠키 제거 → /api/auth/login → IdP 세션 살아있음 → 콜백 →
    // 같은 쿠키 → 무한 루프였다. 화면에는 아무 진단도 없었다.
    configureOidc({ OIDC_COOKIE_TOKEN: 'access_token' });
    stubTokenEndpoint({ id_token: jwtWithExp(null), access_token: 'opaque-okta-token-abc123' });
    const res = await callbackGET(callbackReq());

    expect(res.status).toBe(502);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
    const html = await res.text();
    expect(html).toMatch(/opaque|불투명/);
    // 원인을 알려줘야 운영자가 고칠 수 있다.
    expect(html).toContain('OIDC_SCOPES');
  });

  it('id_token 이 없으면 access_token 으로 폴백한다', async () => {
    configureOidc();
    stubTokenEndpoint({ access_token: jwtWithExp(null) });
    const res = await callbackGET(callbackReq());
    expect(res.status).toBe(303);
    expect(setCookies(res)['admin_jwt']).toBeTruthy();
  });

  it('OIDC_COOKIE_TOKEN=access_token 이면 access_token 을 쿠키에 넣는다', async () => {
    // ⚠️ 두 토큰 모두 **진짜 JWT** 여야 한다. 예전엔 'AC.TOKEN.Y' 같은 더미 문자열을
    //    썼는데, 이제 콜백이 굽기 전에 parseJWT 로 가독성을 검사하므로 더미는 502 가 된다
    //    (그 검사가 무한 리다이렉트를 막는다). 어느 토큰이 선택됐는지는 클레임으로 구분한다.
    configureOidc({ OIDC_COOKIE_TOKEN: 'access_token' });
    const accessJwt = jwtWithExp(null, { which: 'access' });
    stubTokenEndpoint({ id_token: jwtWithExp(null, { which: 'id' }), access_token: accessJwt });
    const res = await callbackGET(callbackReq());
    expect(res.status).toBe(303);
    expect(cookieValue(setCookies(res)['admin_jwt'])).toBe(accessJwt);
  });

  it('https 종단이면 admin_jwt 에 Secure 가 붙는다', async () => {
    configureOidc();
    stubTokenEndpoint({ id_token: jwtWithExp(null) });
    const res = await callbackGET(callbackReq(undefined, { 'x-forwarded-proto': 'https' }));
    expect(setCookies(res)['admin_jwt']).toMatch(/Secure/i);
    // 리다이렉트는 상대 경로라 scheme 이 없다 — 브라우저가 https 오리진을 그대로 쓴다.
    // (Secure 플래그는 x-forwarded-proto 로 판정하므로 위 단정이 그 계약을 지킨다.)
    expectRelativeRedirect(res, '/');
  });
});

describe('GET /api/auth/callback — 실패를 읽히게', () => {
  it('provider 의 error/error_description 을 페이지로 보여준다 (크래시 아님)', async () => {
    configureOidc();
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?error=access_denied&error_description=User+cancelled'),
    );
    expect(res.status).toBe(400);
    const body = await res.text();
    expect(body).toContain('access_denied');
    expect(body).toContain('User cancelled');
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });

  it('error_description 은 escape 된다 (반사형 XSS 금지)', async () => {
    configureOidc();
    const res = await callbackGET(
      req(
        'http://admin.test/api/auth/callback?error=bad&error_description=' +
          encodeURIComponent('<script>alert(1)</script>'),
      ),
    );
    const body = await res.text();
    expect(body).not.toContain('<script>alert(1)</script>');
    expect(body).toContain('&lt;script&gt;');
  });

  it('token 엔드포인트가 400 이면 502 + provider error 코드를 보여준다', async () => {
    configureOidc();
    stubTokenEndpoint({ error: 'invalid_grant' }, 400);
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=C&state=S', {
        cookies: { oidc_state: 'S', oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.text()).toContain('invalid_grant');
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });

  it('token 엔드포인트에 닿지 못하면 502 로 구분해 준다', async () => {
    configureOidc();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('ECONNREFUSED');
      }),
    );
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=C&state=S', {
        cookies: { oidc_state: 'S', oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.text()).toContain('ECONNREFUSED');
  });

  it('토큰이 하나도 없으면 502 (쿠키를 빈 값으로 세우지 않는다)', async () => {
    configureOidc();
    stubTokenEndpoint({ token_type: 'Bearer' });
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=C&state=S', {
        cookies: { oidc_state: 'S', oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(502);
    expect(setCookies(res)['admin_jwt']).toBeUndefined();
  });

  it('OIDC 미설정 상태로 콜백이 오면 503 (state 통과 후에도 크래시 없음)', async () => {
    const res = await callbackGET(
      req('http://admin.test/api/auth/callback?code=C&state=S', {
        cookies: { oidc_state: 'S', oidc_verifier: 'v'.repeat(43) },
      }),
    );
    expect(res.status).toBe(503);
    expect(await res.text()).toContain('OIDC_TOKEN_URL');
  });
});

// ───────────────────────── 3. middleware 목적지 ─────────────────────────

/**
 * 미들웨어 리다이렉트는 라우트 핸들러와 계약이 **반대**다: 같은 오리진의 절대 URL.
 * 상대 Location 을 내면 Next 14 미들웨어 어댑터가 `new NextURL(location)` 에서
 * Invalid URL 로 던져 그 요청이 500 이 된다(2026-10-14 dev·prod 실측). 같은 오리진이면
 * Next 서버가 보내기 전에 상대 경로로 바꾸므로, 브라우저가 받는 Location 은 여전히 상대다.
 */
function expectMiddlewareRedirect(res: Response, path: string, origin = 'http://admin.test'): void {
  const loc = res.headers.get('location');
  expect(loc).not.toBeNull();
  expect(() => new NextURL(loc!, { forceLocale: false })).not.toThrow();
  const url = new URL(loc!);
  expect(url.origin).toBe(origin); // 요청 오리진만 — 다른 호스트로는 절대 안 나간다
  expect(url.pathname).toBe(path);
  expect(url.search).toBe('');
}

describe('middleware — 로그인 목적지는 /api/auth/login 이다', () => {
  function b64(obj: unknown): string {
    return Buffer.from(JSON.stringify(obj)).toString('base64url');
  }

  it('쿠키 없음 → /api/auth/login (dev-login 직행 금지)', async () => {
    const res = await middleware(req('http://admin.test/'));
    expect(res.status).toBe(307);
    expectMiddlewareRedirect(res, '/api/auth/login');
    expect(new URL(res.headers.get('location')!).pathname).not.toBe('/api/auth/dev-login');
  });

  it('만료된 토큰 → /api/auth/login + admin_jwt 제거', async () => {
    const expired = `header.${b64({ sub: 'u', role: 'ADMIN', exp: Math.floor(Date.now() / 1000) - 60 })}.sig`;
    const res = await middleware(req('http://admin.test/', { cookies: { admin_jwt: expired } }));
    expectMiddlewareRedirect(res, '/api/auth/login');
    const setCookie = res.headers.get('set-cookie') ?? '';
    expect(setCookie).toContain('admin_jwt=');
    expect(setCookie).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
  });

  it('손상된 토큰 → /api/auth/login + admin_jwt 제거', async () => {
    const res = await middleware(req('http://admin.test/', { cookies: { admin_jwt: 'not-a-jwt' } }));
    expectMiddlewareRedirect(res, '/api/auth/login');
    expect(res.headers.get('set-cookie') ?? '').toContain('admin_jwt=');
  });

  it('/api/auth/login 자체는 미인증으로 도달 가능하다 (무한 리다이렉트 방지)', async () => {
    // middleware.ts 의 `/api/` 예외에 걸려야 한다. 안 걸리면 로그인 진입점이
    // 자기 자신으로 리다이렉트되며 ERR_TOO_MANY_REDIRECTS 가 된다.
    const res = await middleware(req('http://admin.test/api/auth/login'));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('/api/auth/callback 도 미인증으로 도달 가능하다', async () => {
    const res = await middleware(req('http://admin.test/api/auth/callback?code=C&state=S'));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('요청 오리진만 싣고 쿼리는 버린다', async () => {
    // 미들웨어가 싣는 호스트는 **요청 자신의 오리진**뿐이다. 그래야 Next 서버가 같은
    // 오리진으로 보고 상대 Location 으로 바꿔 보낸다 — 내부 호스트가 브라우저로 새지 않는다.
    // (예전 "상대 Location 을 직접 낸다" 는 어댑터에서 500 이었다.) 쿼리를 버리는 성질은
    // 그대로 지킨다 — 로그인 진입점에 원래 요청의 쿼리가 새어 들어갈 이유가 없다.
    const res = await middleware(req('http://admin.internal:3000/budgets?q=secret'));
    expectMiddlewareRedirect(res, '/api/auth/login', 'http://admin.internal:3000');
    expect(res.headers.get('location')).not.toContain('secret');
  });
});

// ───────────────────────── 4. dev-login 비활성 응답 ─────────────────────────

describe('/api/auth/dev-login — 꺼져 있을 때', () => {
  it('GET → 503 + 읽히는 본문 (본문 없는 404 금지)', async () => {
    delete process.env.DEV_LOGIN_ENABLED;
    const res = await devLoginGET();

    // ⚠️ A3 의 회귀 가드. 예전 값: status 404 / body ''.
    expect(res.status).not.toBe(404);
    expect(res.status).toBe(503);
    const body = await res.text();
    expect(body.length).toBeGreaterThan(0);
    expect(body).toContain('/api/auth/login');
    expect(res.headers.get('content-type')).toContain('text/html');
  });

  it('POST → 503 JSON + SSO 진입점 안내', async () => {
    delete process.env.DEV_LOGIN_ENABLED;
    const res = await devLoginPOST(
      new NextRequest('http://admin.test/api/auth/dev-login', {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: 'role=ADMIN',
      }),
    );
    expect(res.status).toBe(503);
    const json = (await res.json()) as { error?: string; sso_entry_point?: string };
    expect(json.sso_entry_point).toBe('/api/auth/login');
    expect(json.error).toContain('/api/auth/login');
  });
});

describe('/api/auth/dev-login — 켜져 있을 때는 오늘과 동일 (dev 무변경)', () => {
  it('GET → 200 HTML 폼', async () => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await devLoginGET();
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain('<form method="POST" action="/api/auth/dev-login">');
    expect(body).toContain('>ADMIN<');
    expect(body).toContain('>TEAM_LEADER<');
  });

  it('POST → dev.<b64>.sig 토큰을 24h httpOnly 쿠키로 세운다', async () => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await devLoginPOST(
      new NextRequest('http://admin.test/api/auth/dev-login', {
        method: 'POST',
        headers: {
          'content-type': 'application/x-www-form-urlencoded',
          host: 'admin.test',
        },
        body: 'role=ADMIN',
      }),
    );

    // ⚠️ 303 이어야 한다(예전엔 307). 이 요청은 **POST** 이고, 307 은 메서드를 보존하므로
    //    브라우저가 `/` 로 다시 POST 한다 — `/` 는 페이지(GET) 라서 그 요청은 의미가 없다.
    //    303 은 "결과를 GET 으로 보라" 는 뜻이고, logout 라우트가 이미 그렇게 하고 있었다.
    expect(res.status).toBe(303);
    expectRelativeRedirect(res, '/');
    const jar = setCookies(res);
    const token = cookieValue(jar['admin_jwt']);
    expect(token.startsWith('dev.')).toBe(true);
    expect(token.split('.').length).toBe(3);
    const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString()) as {
      role: string;
      email: string;
    };
    expect(payload.role).toBe('ADMIN');
    expect(payload.email).toBe('admin@dev.local');
    expect(Number(/Max-Age=(\d+)/i.exec(jar['admin_jwt'])![1])).toBe(86400);
    expect(jar['admin_jwt']).toMatch(/HttpOnly/i);
  });

  it('POST 잘못된 role → 400 (기존 검증 유지)', async () => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    const res = await devLoginPOST(
      new NextRequest('http://admin.test/api/auth/dev-login', {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: 'role=SUPERUSER',
      }),
    );
    expect(res.status).toBe(400);
  });
});
