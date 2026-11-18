// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 로그인 때 admin-api 가 답한 역할과 팀(`GET /admin/me`)을 세션 동안 기억하는 쿠키(`admin_role`).
 *
 * 왜 필요한가: IdP 토큰에는 `role`·`team_id` 클레임이 없다. 토큰만으로는 관리자 그룹 → ADMIN 밖에 못
 * 정하고(lib/auth.ts resolveRole), admin-api 는 이메일과 DB 역할(TEAM_LEADER 등)로도 인가하므로 그런
 * 사용자는 API 는 통과하는데 모든 화면이 /403 이었다. 팀이 없으면 팀 리더의 예산 화면이 비었다.
 *
 * ⚠️ 값: `<ROLE>.<base64url(SHA-256(토큰)) 앞 22자>[.<team UUID>]`. 지문으로 토큰에 묶는다 — 로그아웃·
 *    계정 전환·dev 로그인으로 admin_jwt 가 바뀌면 남은 쿠키는 버려져, 한 세션의 역할·팀이 다른 토큰에
 *    붙는 일은 없다.
 * ⚠️ 화면 판정용 참고값일 뿐이다. 서명이 없어 본인이 고쳐 쓸 수 있지만 데이터 호출은 전부 admin-api 가
 *    다시 인가한다(lib/auth.ts 가 서명 검증 없이 `role` 클레임을 믿는 것과 같은 이유). 팀도 매 호출
 *    검사한다 — 다른 팀의 `/admin/budgets/team/{id}/...` 는 거절된다.
 *
 * middleware(Edge)와 라우트 핸들러(Node) 양쪽에서 돈다 — 전역 `crypto.subtle` 만 쓴다.
 */

import { UserRole } from '@/types/enums';

export const ROLE_COOKIE_NAME = 'admin_role';

const KNOWN_ROLES: readonly string[] = Object.values(UserRole);

/** 지문 길이(base64url 문자 수). 22자 = 132비트. */
const FINGERPRINT_LENGTH = 22;

const FINGERPRINT_SHAPE = new RegExp(`^[A-Za-z0-9_-]{${FINGERPRINT_LENGTH}}$`);

/** 정규 UUID(8-4-4-4-12, 대소문자 무관) — admin-api 의 team_id 형식. */
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function fingerprint(token: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  let binary = '';
  for (const b of new Uint8Array(digest)) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').slice(0, FINGERPRINT_LENGTH);
}

export async function encodeRoleCookie(
  role: UserRole,
  token: string,
  teamId?: string | null,
): Promise<string> {
  const value = `${role}.${await fingerprint(token)}`;
  // UUID 가 아닌 팀은 싣지 않는다 — 실으면 readRoleCookie 가 쿠키 전체를 버려 역할까지 잃는다.
  return teamId && UUID_SHAPE.test(teamId) ? `${value}.${teamId}` : value;
}

/** 이 토큰에 묶인 올바른 값일 때만 역할과 팀을 돌려준다. 그 밖에는 undefined 이고 던지지 않는다. */
export async function readRoleCookie(
  value: string | undefined,
  token: string,
): Promise<{ role: UserRole; teamId: string | null } | undefined> {
  try {
    if (typeof value !== 'string') return undefined;
    const parts = value.split('.');
    if (parts.length !== 2 && parts.length !== 3) return undefined;
    const [role, print, teamId] = parts;
    if (!KNOWN_ROLES.includes(role) || !FINGERPRINT_SHAPE.test(print)) return undefined;
    // 셋째 조각은 UUID 여야 한다. 아니면 "역할만" 으로 물러서지 않고 쿠키 전체를 버린다.
    if (teamId !== undefined && !UUID_SHAPE.test(teamId)) return undefined;
    if (print !== (await fingerprint(token))) return undefined;
    return { role: role as UserRole, teamId: teamId ?? null };
  } catch {
    // 지문을 못 구하는 런타임 — 역할을 모르는 것으로 본다(fail-closed).
    return undefined;
  }
}
