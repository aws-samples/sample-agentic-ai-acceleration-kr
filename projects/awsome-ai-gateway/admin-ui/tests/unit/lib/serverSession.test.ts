// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * getServerSession(lib/serverSession.ts) — 레이아웃과 예산 화면이 middleware 와 같은 역할·팀을 본다.
 *
 * 이게 parseJWT 만 부르면 middleware 는 역할 쿠키로 들여보냈는데 레이아웃은 역할을 몰라 사이드바에
 * 대시보드만 남고, 이메일 관리자는 예산 화면에서 관리자 표 대신 "팀 없음" 을 본다. IdP 토큰에는 팀도
 * 없어서, 쿠키의 팀이 없으면 팀 리더의 예산 화면도 "팀 없음" 이다.
 */

// @vitest-environment node

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const jar = vi.hoisted(() => new Map<string, string>());

vi.mock('next/headers', () => ({
  cookies: () => ({
    get: (name: string) => (jar.has(name) ? { name, value: jar.get(name)! } : undefined),
  }),
}));

import { encodeRoleCookie } from '@/lib/roleCookie';
import { getServerSession } from '@/lib/serverSession';

function token(payload: Record<string, unknown>): string {
  return `header.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.sig`;
}

/** IdP id_token 모양 — role 도 team_id 도 없다. */
const IDP = token({ sub: 'idp-subject', email: 'lead@example.com', groups: ['Developers'] });
const TEAM = '0b7c6f1e-3a52-4c1d-9e8f-2a4b6c8d0e1f';
const OTHER_TEAM = '5d2e9a40-7b1c-4f3e-8a6d-1c9b0e7f2a3d';

beforeEach(() => {
  jar.clear();
  vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('getServerSession', () => {
  it('admin_jwt 가 없으면 null', async () => {
    expect(await getServerSession()).toBeNull();
  });

  it('못 읽는 토큰이면 null (던지지 않는다)', async () => {
    jar.set('admin_jwt', 'not-a-jwt');
    expect(await getServerSession()).toBeNull();
  });

  it('IdP 토큰 + 그 토큰에 묶인 TEAM_LEADER·팀 쿠키 → 그 역할과 그 팀', async () => {
    jar.set('admin_jwt', IDP);
    jar.set('admin_role', await encodeRoleCookie('TEAM_LEADER', IDP, TEAM));
    expect(await getServerSession()).toMatchObject({
      role: 'TEAM_LEADER',
      email: 'lead@example.com',
      team_id: TEAM,
    });
  });

  it('쿠키에 팀이 없으면 team_id 는 null', async () => {
    jar.set('admin_jwt', IDP);
    jar.set('admin_role', await encodeRoleCookie('TEAM_LEADER', IDP));
    expect(await getServerSession()).toMatchObject({ role: 'TEAM_LEADER', team_id: null });
  });

  it('역할 쿠키가 없으면 역할도 팀도 없음 (오늘과 같다)', async () => {
    jar.set('admin_jwt', IDP);
    const session = await getServerSession();
    expect(session).not.toBeNull();
    expect(session!.role).toBeUndefined();
    expect(session!.team_id).toBeNull();
  });

  it('role 과 team_id 를 가진 토큰은 쿠키의 역할·팀을 무시한다', async () => {
    const internal = token({ sub: 'u', role: 'TEAM_LEADER', team_id: TEAM });
    jar.set('admin_jwt', internal);
    jar.set('admin_role', await encodeRoleCookie('ADMIN', internal, OTHER_TEAM));
    expect(await getServerSession()).toMatchObject({ role: 'TEAM_LEADER', team_id: TEAM });
  });

  it('dev 로그인 토큰은 role 클레임 그대로', async () => {
    const dev = `dev.${Buffer.from(JSON.stringify({ user_id: 'dev-admin', role: 'ADMIN' })).toString('base64url')}.sig`;
    jar.set('admin_jwt', dev);
    jar.set('admin_role', await encodeRoleCookie('DEVELOPER', dev, TEAM));
    expect(await getServerSession()).toMatchObject({ role: 'ADMIN', team_id: null });
  });
});
