// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * middleware — 만료 분기가 실제로 배선돼 있는지(순수 함수 테스트만으로는 증명 안 됨).
 *
 * 회귀: 만료된 admin_jwt 는 "쿠키가 있다"는 이유로 미인증 분기를 타지 않아 페이지가
 * 그대로 열렸고, 서버 컴포넌트의 admin-api 호출만 전부 401 이 됐다. 사용자에게는
 * 로그인으로 돌아갈 길이 없었다.
 */

import { describe, it, expect } from 'vitest';
import { NextRequest } from 'next/server';
import { NextURL } from 'next/dist/server/web/next-url';
import { middleware } from '@/middleware';

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

function tokenWithExp(offsetSeconds: number): string {
  return `header.${b64url({
    sub: '11111111-1111-1111-1111-111111111111',
    role: 'ADMIN',
    exp: Math.floor(Date.now() / 1000) + offsetSeconds,
  })}.sig`;
}

const DEV_TOKEN = `dev.${b64url({
  user_id: 'dev-admin',
  email: 'admin@dev.local',
  display_name: 'Dev Admin',
  role: 'ADMIN',
  team_id: null,
  department_id: null,
  issued_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 86_400_000).toISOString(),
})}.sig`;

function requestWith(cookie?: string, pathname = '/'): NextRequest {
  const req = new NextRequest(`http://admin.test${pathname}`);
  if (cookie !== undefined) {
    req.cookies.set('admin_jwt', cookie);
  }
  return req;
}

// ⚠️ 리다이렉트 목적지는 `/api/auth/dev-login` 이 아니라 `/api/auth/login` 이다.
//    prod 는 `DEV_LOGIN_ENABLED=false` 라 dev-login GET 이 404 였고, admin_jwt 를 굽는
//    코드가 레포에 dev-login 뿐이어서 **prod 에는 로그인 경로가 아예 없었다**(미인증
//    브라우저 → 307 → 바디 없는 404 막다른 길). 이제 middleware 는 환경을 보고
//    OIDC authorize / dev 폼 / 읽히는 503 으로 갈라주는 단일 진입점 `/api/auth/login`
//    으로 보낸다(src/app/api/auth/login/route.ts).
/**
 * 미들웨어 리다이렉트의 계약: Location 은 **같은 오리진의 절대 URL** 이다.
 *
 * 예전 계약(상대 Location)은 이 파일에서는 통과했지만 실제 런타임에서 모든 페이지를 500
 * 으로 만들었다. 이 테스트는 `middleware()` 를 직접 부르지만, 실제로는 그 반환값을 Next 의
 * 미들웨어 어댑터가 받아 Location 을 `new NextURL(location)` 으로 **base 없이** 다시
 * 파싱한다 — 상대 경로면 `TypeError: Invalid URL` (2026-10-14 dev·prod 실측, next 14.2.29).
 * 그래서 여기서 **어댑터와 같은 파서**로 Location 을 다시 만들어 본다.
 *
 * 같은 오리진이어야 하는 이유: 어댑터는 요청과 같은 호스트의 URL 만 남기고, 서버가 그
 * 리다이렉트를 상대 경로로 바꿔 보낸다 — 브라우저가 받는 것은 여전히 상대 Location 이라
 * CloudFront 오리진이 유지된다(lib/redirect.ts 의 우려는 그대로 해소된다).
 */
function expectSameOriginRedirect(res: Response, path: string): void {
  const loc = res.headers.get('location');
  expect(loc).not.toBeNull();
  // 어댑터가 하는 연산 그대로 — 여기서 던지면 그 요청은 실서버에서 500 이다.
  expect(() => new NextURL(loc!, { forceLocale: false })).not.toThrow();
  const url = new URL(loc!);
  expect(url.origin).toBe('http://admin.test');
  expect(url.pathname).toBe(path);
}

describe('middleware — Location survives the Next adapter', () => {
  it('the parser used above really rejects a relative Location (대조군)', () => {
    // 이 단정이 없으면 위 검사는 "NextURL 이 원래 관대하다" 일 때도 통과해 공허해진다.
    expect(() => new NextURL('/api/auth/login', { forceLocale: false })).toThrow();
  });

  it('an unauthenticated page request gets an adapter-safe redirect', async () => {
    const res = await middleware(requestWith(undefined, '/settings'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });
});

describe('middleware — session expiry', () => {
  it('redirects an EXPIRED token to login and clears the stale cookie', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600)));

    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
    // 못 쓰는 자격증명은 응답에서 제거돼야 한다.
    const setCookie = res.headers.get('set-cookie') ?? '';
    expect(setCookie).toContain('admin_jwt=');
    expect(setCookie).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
  });

  it('lets a LIVE token through', async () => {
    const res = await middleware(requestWith(tokenWithExp(3600)));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a dev-login token through — it carries no numeric exp', async () => {
    const res = await middleware(requestWith(DEV_TOKEN));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a token with NO expiry claim at all through (진짜 fail-open 분기)', async () => {
    // ⚠️ 위의 dev-login 테스트는 이 분기를 덮지 못한다. DEV_TOKEN 에는 ISO `expires_at` 이
    //    24시간 뒤로 들어 있어서 isSessionExpired 의 **ISO 파싱 분기**를 타고 false 가 된다.
    //    exp/expires_at 이 아예 없을 때의 관용(`if (!raw) return false`)은 그 테스트가
    //    통과해도 배선되지 않았을 수 있다 — 그러면 그런 토큰을 들고 온 사용자는 로그인
    //    직후 다시 로그인으로 튕겨 무한 리다이렉트에 빠진다. 그 분기를 직접 찍는다.
    const res = await middleware(requestWith(`dev.${b64url({ role: 'ADMIN' })}.sig`));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a token with an UNPARSEABLE expiry through — 판정 보류', async () => {
    // 만료를 확신할 수 없을 때 튕겨내면 정상 사용자를 잠글 수 있다.
    const res = await middleware(
      requestWith(`dev.${b64url({ role: 'ADMIN', expires_at: 'not-a-date' })}.sig`),
    );
    expect(res.status).toBe(200);
  });

  it('redirects when the expiry is inside the skew window (UI 가 먼저 만료로 본다)', async () => {
    // admin-api 의 jose leeway 는 0 이라(admin-api/src/app/core/auth.py:63) exp 시점에
    // 정확히 401 이 된다. UI 가 그보다 늦게 만료로 보면 그 구간에서 '페이지는 열리는데
    // 전부 401' 이라는 원래 증상이 재현된다. 10초 남은 토큰은 이미 로그인으로 보내야 한다.
    const res = await middleware(requestWith(tokenWithExp(10)));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('still redirects when no cookie is present (and does not clear anything)', async () => {
    const res = await middleware(requestWith(undefined));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('redirects a malformed token to login and clears the cookie', async () => {
    const res = await middleware(requestWith('not-a-jwt'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
    expect(res.headers.get('set-cookie') ?? '').toContain('admin_jwt=');
  });

  it('applies security headers to the expiry redirect too', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600)));
    expect(res.headers.get('X-Frame-Options')).toBe('DENY');
    expect(res.headers.get('X-Content-Type-Options')).toBe('nosniff');
  });

  it('403 stays public — an expired cookie must not bounce it into a redirect loop', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600), '/403'));
    expect(res.status).toBe(200);
  });
});

describe('middleware — removed screens are not public', () => {
  // /analytics 와 /cli 화면은 제거됐다. 예전 공개 분기 `startsWith('/cli')` 는 접두사만 봐서
  // `/clients` 같은 경로까지 무인증으로 통과시켰다 — 남은 소비자가 없으니 일반 경로처럼 막는다.
  it.each(['/cli', '/cli/download/linux/amd64', '/clients', '/analytics', '/analytics/models'])(
    '%s without a cookie → login',
    async (path) => {
      const res = await middleware(requestWith(undefined, path));
      expectSameOriginRedirect(res, '/api/auth/login');
    },
  );

  it.each(['/cli', '/analytics', '/analytics/models'])(
    '%s with a live ADMIN token → /403 (권한표 미등재 = default-deny)',
    async (path) => {
      const res = await middleware(requestWith(tokenWithExp(3600), path));
      expectSameOriginRedirect(res, '/403');
    },
  );
});
