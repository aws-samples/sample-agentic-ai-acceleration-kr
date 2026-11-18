// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 역할 쿠키(lib/roleCookie.ts)와 resolveSession(lib/auth.ts).
 *
 * 배경: IdP 토큰에는 role·team_id 클레임이 없어서 이메일 관리자와 팀 리더는 API 는 통과하는데 모든
 * 화면이 /403 이었고, 팀 리더의 예산 화면은 비었다. 로그인 콜백이 admin-api 에서 받은 역할과 팀을
 * `admin_role` 쿠키로 기억한다.
 *
 * 여기서 고정하는 것:
 *   1. 쿠키는 **그 토큰에만** 통한다 — 다른 토큰(로그아웃·계정 전환·dev 로그인 뒤)에는 안 붙는다.
 *   2. 모양이 조금이라도 틀리면(팀 조각 포함) 통째로 버린다 — "역할만" 으로 물러서지 않는다. 던지지 않는다.
 *   3. 토큰이 역할을 주면 쿠키보다 먼저다 — dev 로그인·내부 토큰·관리자 그룹은 예전과 같다(팀도 토큰 것).
 */

// @vitest-environment node

import { createHash } from 'node:crypto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { parseJWT, resolveSession } from '@/lib/auth';
import { ROLE_COOKIE_NAME, encodeRoleCookie, readRoleCookie } from '@/lib/roleCookie';

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

function token(payload: Record<string, unknown>): string {
  return `header.${b64url(payload)}.sig`;
}

/** 구현을 베끼지 않고 node:crypto 로 다시 계산한 지문. */
function expectedFingerprint(t: string): string {
  return createHash('sha256').update(t).digest('base64url').slice(0, 22);
}

const ROLES = ['ADMIN', 'TEAM_LEADER', 'DEVELOPER'] as const;

const TEAM = '0b7c6f1e-3a52-4c1d-9e8f-2a4b6c8d0e1f';
const OTHER_TEAM = '5d2e9a40-7b1c-4f3e-8a6d-1c9b0e7f2a3d';

const TOKEN_A = token({ sub: 'user-a', email: 'a@example.com' });
const TOKEN_B = token({ sub: 'user-b', email: 'b@example.com' });

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('roleCookie', () => {
  it('쿠키 이름은 admin_role 이다', () => {
    expect(ROLE_COOKIE_NAME).toBe('admin_role');
  });

  it.each(ROLES)('%s (팀 없음) 는 같은 토큰으로 되읽힌다', async (role) => {
    const value = await encodeRoleCookie(role, TOKEN_A);
    expect(value).toBe(`${role}.${expectedFingerprint(TOKEN_A)}`);
    expect(await readRoleCookie(value, TOKEN_A)).toEqual({ role, teamId: null });
  });

  it.each(ROLES)('%s + 팀 은 같은 토큰으로 되읽힌다', async (role) => {
    const value = await encodeRoleCookie(role, TOKEN_A, TEAM);
    expect(value).toBe(`${role}.${expectedFingerprint(TOKEN_A)}.${TEAM}`);
    expect(await readRoleCookie(value, TOKEN_A)).toEqual({ role, teamId: TEAM });
  });

  it('대문자 UUID 도 받는다', async () => {
    const upper = TEAM.toUpperCase();
    const value = await encodeRoleCookie('TEAM_LEADER', TOKEN_A, upper);
    expect(value.endsWith(`.${upper}`)).toBe(true);
    expect(await readRoleCookie(value, TOKEN_A)).toEqual({ role: 'TEAM_LEADER', teamId: upper });
  });

  it.each([null, undefined, ''])('팀이 %j 이면 두 조각으로 쓴다', async (teamId) => {
    const value = await encodeRoleCookie('TEAM_LEADER', TOKEN_A, teamId);
    expect(value).toBe(`TEAM_LEADER.${expectedFingerprint(TOKEN_A)}`);
  });

  it.each([
    'team-1',
    `{${TEAM}}`,
    TEAM.replace(/-/g, ''),
    `${TEAM} `,
    `g${TEAM.slice(1)}`,
    `${TEAM}.x`,
  ])('UUID 가 아닌 팀(%j)은 싣지 않는다 — 읽는 쪽이 버릴 값을 쓰지 않는다', async (teamId) => {
    const value = await encodeRoleCookie('TEAM_LEADER', TOKEN_A, teamId);
    expect(value).toBe(`TEAM_LEADER.${expectedFingerprint(TOKEN_A)}`);
    // 역할은 그대로 살아남는다.
    expect(await readRoleCookie(value, TOKEN_A)).toEqual({ role: 'TEAM_LEADER', teamId: null });
  });

  it('두 토큰의 지문이 다르다 (대조군 — 아래 거부 단정이 공허하지 않다)', async () => {
    expect(expectedFingerprint(TOKEN_A)).not.toBe(expectedFingerprint(TOKEN_B));
    expect(await encodeRoleCookie('ADMIN', TOKEN_A)).not.toBe(
      await encodeRoleCookie('ADMIN', TOKEN_B),
    );
  });

  describe.each([
    ['팀 없음', undefined],
    ['팀 있음', TEAM],
  ])('다른 토큰에 묶인 쿠키는 버린다 (%s)', (_label, teamId) => {
    it.each(ROLES)('%s', async (role) => {
      const boundToA = await encodeRoleCookie(role, TOKEN_A, teamId);
      expect(await readRoleCookie(boundToA, TOKEN_A)).toBeDefined(); // 대조군: 제 토큰으로는 읽힌다
      expect(await readRoleCookie(boundToA, TOKEN_B)).toBeUndefined();
    });
  });

  describe('모양이 틀리면 버린다 — 지문은 맞고 한 군데만 틀린 값', () => {
    const fp = expectedFingerprint(TOKEN_A);
    it.each([
      ['undefined', undefined],
      ['빈 문자열', ''],
      ['쓰레기', 'garbage'],
      ['역할만', 'ADMIN'],
      ['지문 없음', 'ADMIN.'],
      ['역할 없음', `.${fp}`],
      ['모르는 역할', `SUPERUSER.${fp}`],
      ['소문자 역할', `admin.${fp}`],
      ['구분자 콜론', `ADMIN:${fp}`],
      ['구분자 밑줄', `ADMIN_${fp}`],
      ['구분자 공백', `ADMIN ${fp}`],
      ['점 두 개', `ADMIN..${fp}`],
      ['지문 짧음', `ADMIN.${fp.slice(0, 21)}`],
      ['지문 김', `ADMIN.${fp}A`],
      ['지문 전체(43자)', `ADMIN.${createHash('sha256').update(TOKEN_A).digest('base64url')}`],
      ['표준 base64 문자', `ADMIN.${'+'.repeat(22)}`],
      ['앞 공백', ` ADMIN.${fp}`],
      ['뒤 공백', `ADMIN.${fp} `],
      ['팀 조각이 UUID 아님', `TEAM_LEADER.${fp}.x`],
      ['빈 팀 조각', `TEAM_LEADER.${fp}.`],
      ['넷째 조각', `TEAM_LEADER.${fp}.${TEAM}.x`],
      ['빈 넷째 조각', `TEAM_LEADER.${fp}.${TEAM}.`],
      ['팀 하이픈 없음', `TEAM_LEADER.${fp}.${TEAM.replace(/-/g, '')}`],
      ['팀 중괄호', `TEAM_LEADER.${fp}.{${TEAM}}`],
      ['팀 짧음', `TEAM_LEADER.${fp}.${TEAM.slice(0, -1)}`],
      ['팀 16진수 아님', `TEAM_LEADER.${fp}.g${TEAM.slice(1)}`],
      ['팀 앞 공백', `TEAM_LEADER.${fp}. ${TEAM}`],
      ['팀이 지문 자리에', `TEAM_LEADER.${TEAM}`],
      ['지문 없이 팀만', `TEAM_LEADER..${TEAM}`],
    ])('%s', async (_label, value) => {
      expect(await readRoleCookie(value, TOKEN_A)).toBeUndefined();
    });
  });

  describe('던지지 않는다', () => {
    it('문자열이 아닌 값', async () => {
      for (const value of [123, {}, null, ['ADMIN']]) {
        await expect(
          readRoleCookie(value as unknown as string, TOKEN_A),
        ).resolves.toBeUndefined();
      }
    });

    it('토큰이 문자열이 아니어도', async () => {
      const value = await encodeRoleCookie('ADMIN', TOKEN_A);
      await expect(readRoleCookie(value, undefined as unknown as string)).resolves.toBeUndefined();
    });

    it('지문을 못 구하는 런타임이면 역할을 모르는 것으로 본다', async () => {
      const value = await encodeRoleCookie('ADMIN', TOKEN_A);
      const digest = vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('subtle unavailable'));
      await expect(readRoleCookie(value, TOKEN_A)).resolves.toBeUndefined();
      expect(digest).toHaveBeenCalled(); // 스파이가 실제로 끼어들었다(공허성 대조)
    });
  });
});

describe('resolveSession — 토큰이 먼저, 쿠키는 그다음', () => {
  it.each(ROLES)('role 클레임(%s)은 어떤 쿠키보다 이긴다', async (claimed) => {
    const t = token({ sub: 'u', role: claimed });
    for (const cookieRole of ROLES) {
      const cookie = await encodeRoleCookie(cookieRole, t, TEAM);
      const session = await resolveSession(t, cookie);
      expect(session.role).toBe(claimed);
      expect(session.team_id).toBeNull(); // 토큰을 그대로 돌려준다 — 쿠키의 팀도 붙이지 않는다
    }
  });

  it('role 과 team_id 를 가진 토큰은 쿠키의 팀을 무시한다', async () => {
    const t = token({ sub: 'u', role: 'TEAM_LEADER', team_id: TEAM });
    const session = await resolveSession(t, await encodeRoleCookie('TEAM_LEADER', t, OTHER_TEAM));
    expect(session.role).toBe('TEAM_LEADER');
    expect(session.team_id).toBe(TEAM);
  });

  it('dev 로그인 토큰은 예전과 같다', async () => {
    const dev = `dev.${b64url({ user_id: 'dev-admin', role: 'TEAM_LEADER', team_id: null })}.sig`;
    const cookie = await encodeRoleCookie('ADMIN', dev, TEAM);
    expect(await resolveSession(dev, cookie)).toEqual(parseJWT(dev));
    expect(await resolveSession(dev)).toEqual(parseJWT(dev));
  });

  it('관리자 그룹 IdP 토큰은 쿠키 없이 ADMIN — 낮은 역할 쿠키로 강등되지 않고 팀도 붙지 않는다', async () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    vi.stubEnv('OIDC_GROUPS_CLAIM', 'cognito:groups');
    const t = token({ sub: 'u', 'cognito:groups': ['GatewayAdmin'] });

    expect((await resolveSession(t)).role).toBe('ADMIN');
    const withCookie = await resolveSession(t, await encodeRoleCookie('DEVELOPER', t, TEAM));
    expect(withCookie.role).toBe('ADMIN');
    expect(withCookie.team_id).toBeNull();
  });

  describe('관리자 그룹이 아닌 IdP 토큰', () => {
    const idp = token({ sub: 'idp-subject', email: 'leader@example.com', groups: ['Developers'] });

    it('쿠키가 없으면 undefined (오늘과 같은 fail-closed)', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      expect(parseJWT(idp).role).toBeUndefined(); // 전제: 토큰만으로는 역할이 없다
      expect(parseJWT(idp).team_id).toBeNull(); // 전제: 팀도 없다
      const session = await resolveSession(idp);
      expect(session.role).toBeUndefined();
      expect(session.team_id).toBeNull();
    });

    it.each(ROLES)('이 토큰에 묶인 %s 쿠키면 그 역할', async (role) => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      expect((await resolveSession(idp, await encodeRoleCookie(role, idp))).role).toBe(role);
    });

    it('TEAM_LEADER + 팀 쿠키 → 그 역할과 그 팀', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const session = await resolveSession(idp, await encodeRoleCookie('TEAM_LEADER', idp, TEAM));
      expect(session.role).toBe('TEAM_LEADER');
      expect(session.team_id).toBe(TEAM);
    });

    it('TEAM_LEADER 쿠키에 팀이 없으면 team_id 는 null', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const session = await resolveSession(idp, await encodeRoleCookie('TEAM_LEADER', idp));
      expect(session.role).toBe('TEAM_LEADER');
      expect(session.team_id).toBeNull();
    });

    it('토큰에 자기 team_id 가 있으면 그것을 쓴다', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const withTeam = token({ sub: 'idp-subject', groups: ['Developers'], team_id: TEAM });
      const session = await resolveSession(
        withTeam,
        await encodeRoleCookie('TEAM_LEADER', withTeam, OTHER_TEAM),
      );
      expect(session.role).toBe('TEAM_LEADER');
      expect(session.team_id).toBe(TEAM);
    });

    it('다른 토큰에 묶인 쿠키면 역할도 팀도 없다', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const other = token({ sub: 'someone-else', groups: ['Developers'] });
      const session = await resolveSession(idp, await encodeRoleCookie('ADMIN', other, TEAM));
      expect(session.role).toBeUndefined();
      expect(session.team_id).toBeNull();
    });

    it('팀 조각이 깨진 쿠키는 역할도 주지 않는다 ("역할만" 으로 물러서지 않음)', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const broken = `${await encodeRoleCookie('TEAM_LEADER', idp)}.not-a-uuid`;
      const session = await resolveSession(idp, broken);
      expect(session.role).toBeUndefined();
      expect(session.team_id).toBeNull();
    });

    it('역할·팀 말고 다른 필드는 parseJWT 그대로다', async () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      expect(await resolveSession(idp, await encodeRoleCookie('TEAM_LEADER', idp))).toEqual({
        ...parseJWT(idp),
        role: 'TEAM_LEADER',
      });
      expect(await resolveSession(idp, await encodeRoleCookie('TEAM_LEADER', idp, TEAM))).toEqual({
        ...parseJWT(idp),
        role: 'TEAM_LEADER',
        team_id: TEAM,
      });
    });
  });

  it('못 읽는 토큰은 parseJWT 처럼 던진다 (middleware 가 로그인으로 보내는 근거)', async () => {
    await expect(resolveSession('not-a-jwt', 'ADMIN.xxxxxxxxxxxxxxxxxxxxxx')).rejects.toThrow(
      /Invalid JWT format/,
    );
  });
});
