// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * dev 로그인은 개발 환경에서만 열린다 (lib/devLogin.ts).
 *
 * 배경: dev 로그인 폼은 비밀번호 없이 ADMIN 쿠키를 굽는다. 게이트가
 * `DEV_LOGIN_ENABLED === 'true'` 하나였기 때문에, 그 값이 운영 배포에 들어가면 누구나
 * 관리자로 로그인하는 폼이 열렸다.
 *
 * 여기서 고정하는 것:
 *   1. APP_ENV 가 개발 환경이 아니면 스위치가 켜져 있어도 폼도, 쿠키도, 리다이렉트도 없다.
 *   2. 개발 환경에서는 예전과 똑같이 동작한다(대조군) — 이게 없으면 "전부 막아서 통과" 하는
 *      공허한 테스트가 된다.
 *   3. 거부 사유를 구분해서 말한다 — 스위치는 켰는데 환경 때문에 막힌 경우를 "꺼져 있다" 고
 *      답하면 운영자는 원인을 찾지 못한다.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { NextRequest } from 'next/server';
import { GET as loginGET } from '@/app/api/auth/login/route';
import { GET as devLoginGET, POST as devLoginPOST } from '@/app/api/auth/dev-login/route';
import { devLoginState, isDevLoginEnabled } from '@/lib/devLogin';

const VARS = [
  'DEV_LOGIN_ENABLED',
  'APP_ENV',
  'OIDC_CLIENT_ID',
  'OIDC_AUTHORIZE_URL',
  'OIDC_TOKEN_URL',
  'OIDC_CLIENT_SECRET',
  'OIDC_REDIRECT_URI',
  'OIDC_SCOPES',
  'OIDC_COOKIE_TOKEN',
] as const;

const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of VARS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of VARS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

const DEV_ENVS = ['dev', 'development', 'local', 'test', 'DEV', ' Development '];
const NON_DEV_ENVS = ['prod', 'production', 'PROD', 'staging', 'stg', 'qa', 'demo', '', 'developmentx'];

function devLoginPost(): Promise<Response> {
  return devLoginPOST(
    new NextRequest('http://admin.test/api/auth/dev-login', {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded', host: 'admin.test' },
      body: 'role=ADMIN',
    }),
  );
}

// ───────────────────────── 1. 판정 함수 ─────────────────────────

describe('devLoginState', () => {
  it.each(DEV_ENVS)('스위치 on + APP_ENV=%j → 켜짐', (appEnv) => {
    expect(devLoginState({ DEV_LOGIN_ENABLED: 'true', APP_ENV: appEnv })).toEqual({
      enabled: true,
    });
  });

  it.each(NON_DEV_ENVS)('스위치 on + APP_ENV=%j → 환경 때문에 거부', (appEnv) => {
    expect(devLoginState({ DEV_LOGIN_ENABLED: 'true', APP_ENV: appEnv })).toEqual({
      enabled: false,
      reason: 'environment',
      appEnv,
    });
  });

  it('APP_ENV 가 없으면 로컬 실행으로 본다 (admin-api 의 기본값 development 와 같은 판단)', () => {
    expect(isDevLoginEnabled({ DEV_LOGIN_ENABLED: 'true' })).toBe(true);
  });

  it.each([undefined, 'false', 'True', 'TRUE', '1', 'yes', ''])(
    '스위치는 여전히 정확히 "true" 여야 한다 (%j)',
    (flag) => {
      const env = { APP_ENV: 'dev', ...(flag === undefined ? {} : { DEV_LOGIN_ENABLED: flag }) };
      expect(devLoginState(env)).toEqual({ enabled: false, reason: 'switch_off' });
    },
  );
});

// ───────────────────────── 2. 개발 환경이 아닐 때 ─────────────────────────

describe.each(NON_DEV_ENVS)('DEV_LOGIN_ENABLED=true, APP_ENV=%j', (appEnv) => {
  beforeEach(() => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    process.env.APP_ENV = appEnv;
  });

  it('GET /api/auth/dev-login 은 폼을 주지 않는다', async () => {
    const res = await devLoginGET();
    expect(res.status).toBe(503);
    const body = await res.text();
    expect(body).not.toContain('<form');
    expect(body).toContain('APP_ENV=');
    expect(body).toContain('/api/auth/login');
  });

  it('POST /api/auth/dev-login 은 쿠키를 굽지 않는다', async () => {
    const res = await devLoginPost();
    expect(res.status).toBe(503);
    expect(res.headers.get('set-cookie') ?? '').not.toContain('admin_jwt');
    const json = (await res.json()) as { error?: string };
    expect(json.error).toContain('APP_ENV');
    expect(json.error).toContain('refused');
  });

  it('GET /api/auth/login 은 dev 폼으로 보내지 않고 사유를 말한다', async () => {
    const res = await loginGET(new NextRequest('http://admin.test/api/auth/login'));
    expect(res.status).toBe(503);
    expect(res.headers.get('location')).toBeNull();
    const body = await res.text();
    expect(body).toContain('APP_ENV=');
    expect(body).toContain('OIDC_CLIENT_ID');
    // "켜져 있으니 dev-login 을 쓰라" 는 안내가 나가면 안 된다 — 그 라우트는 거부한다.
    expect(body).not.toContain('href="/api/auth/dev-login"');
  });
});

it('APP_ENV 값은 HTML 에 이스케이프되어 나간다', async () => {
  process.env.DEV_LOGIN_ENABLED = 'true';
  process.env.APP_ENV = '<script>alert(1)</script>';
  for (const res of [await devLoginGET(), await loginGET(new NextRequest('http://admin.test/api/auth/login'))]) {
    const body = await res.text();
    expect(body).not.toContain('<script>alert(1)</script>');
    expect(body).toContain('&lt;script&gt;');
  }
});

// ───────────────────────── 3. 대조군 — 개발 환경 ─────────────────────────

describe.each(['dev', 'development', 'local', 'test'])('대조군: DEV_LOGIN_ENABLED=true, APP_ENV=%j', (appEnv) => {
  beforeEach(() => {
    process.env.DEV_LOGIN_ENABLED = 'true';
    process.env.APP_ENV = appEnv;
  });

  it('폼이 열리고, POST 가 dev 쿠키를 굽고, 로그인 진입점이 dev 폼으로 보낸다', async () => {
    const form = await devLoginGET();
    expect(form.status).toBe(200);
    expect(await form.text()).toContain('<form method="POST" action="/api/auth/dev-login">');

    const posted = await devLoginPost();
    expect(posted.headers.get('set-cookie') ?? '').toContain('admin_jwt=dev.');

    const login = await loginGET(new NextRequest('http://admin.test/api/auth/login'));
    expect(login.status).toBe(307);
    expect(login.headers.get('location')).toBe('/api/auth/dev-login');
  });
});
