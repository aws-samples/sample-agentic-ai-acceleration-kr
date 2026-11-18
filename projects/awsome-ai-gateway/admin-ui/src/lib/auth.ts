// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AdminSession } from '@/types/entities';
import type { UserRole } from '@/types/enums';
import { PAGE_PERMISSIONS } from './permissions';
import { readRoleCookie } from './roleCookie';

/**
 * admin-api 와 **같은 이름**의 env 를 읽는다 — 한 Helm 값이 둘을 동시에 먹여야 한다.
 *
 * ⚠️ 이름이 갈리면 관리자 잠김이 난다. 실제 사고 사례: admin-ui 가 관리자 그룹을
 *    하드코딩하고 admin-api 는 설정 가능하게 두어서, 그룹을 개명하는 순간 admin-api 는
 *    인가하는데 admin-ui 가 DEVELOPER 로 판정해 모든 페이지가 /403 으로 튕겼다.
 *
 * ⚠️ `NEXT_PUBLIC_` 접두사를 쓰지 않는다. parseJWT 는 middleware(Edge) 와 RSC 에서만
 *    호출되므로(호출부 4곳 전부 서버) 서버 env 로 충분하고, 관리자 그룹 이름을
 *    클라이언트 번들에 굽지 않는 편이 낫다.
 */
function env(name: string): string {
  return (process.env[name] ?? '').trim();
}

/** admin-api core/config.py `_split_csv` 와 같은 규약: JSON 배열 또는 콤마 구분. */
function envList(name: string): string[] {
  const raw = env(name);
  if (!raw) return [];
  if (raw.startsWith('[')) {
    try {
      const parsed = JSON.parse(raw) as unknown;
      return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : [];
    } catch {
      // admin-api 는 여기서 예외를 던져 부팅을 막지만, UI 가 로그인 자체를 못 하게
      // 만들 이유는 없다 — 잘못된 값은 "관리자 그룹 없음" 으로 취급한다.
      return [];
    }
  }
  return raw.split(',').map((s) => s.trim()).filter(Boolean);
}

/**
 * 토큰에서 역할을 정한다. **admin-api 와 같은 정책**이어야 한다
 * (admin-api/src/app/core/oidc_identity.py `derive_role`).
 *
 * 우선순위:
 *   1. `role` 클레임 — 내부 admin JWT / dev-login 토큰이 갖는다. 그대로 신뢰한다
 *      (서명 검증은 admin-api 몫이고, 여기서 뒤집으면 두 판정이 갈린다).
 *   2. 그룹 매핑 — IdP id_token 에는 `role` 이 **없고** groups 만 있다. groups claim
 *      이름은 IdP 마다 다르므로(Cognito 는 `cognito:groups`) 설정으로 읽는다.
 *   3. 둘 다 없으면 undefined. 그때 resolveSession 이 로그인 때 admin-api 에서 받아 둔 역할
 *      쿠키(lib/roleCookie.ts)를 본다 — 이메일 관리자와 DB 역할(TEAM_LEADER 등)은 토큰에 드러나지
 *      않는다. 그것도 없으면 checkPagePermission 이 거부한다(fail-closed).
 *
 * ⚠️ 2번이 없던 동안 IdP 로그인은 **항상** 관리자 잠김이었다. id_token 에 `role` 이
 *    없으니 role 이 undefined 가 되고, checkPagePermission 은 등재되지 않은 역할을
 *    거부하므로 로그인 직후 모든 페이지가 /403 이었다. admin-api 쪽은 같은 토큰으로
 *    ADMIN 을 인가하고 있었다 — API 는 통과, UI 는 차단이라는 최악의 조합.
 *
 * ⚠️ groups 가 문자열 하나로 오는 IdP 가 있다(단일 값 매퍼). 배열로 정규화하지 않고
 *    `includes` 를 쓰면 **부분 문자열 매칭**이 되어 `GatewayAdminReadOnly` 가
 *    `GatewayAdmin` 을 포함해 읽기 전용 그룹이 ADMIN 으로 승격된다.
 */
function resolveRole(payload: Record<string, unknown>): UserRole {
  const claimed = payload['role'];
  if (typeof claimed === 'string' && claimed) {
    return claimed as UserRole;
  }

  const adminGroups = envList('ADMIN_GROUPS');
  if (adminGroups.length === 0) {
    return undefined as unknown as UserRole;
  }

  const claimName = env('OIDC_GROUPS_CLAIM') || 'groups';
  const raw = payload[claimName];
  const groups: string[] = Array.isArray(raw)
    ? raw.map(String)
    : typeof raw === 'string' && raw
      ? [raw]
      : [];

  // 원소 동등 비교 — 부분 문자열 매칭을 만들지 않는다(위 주석 참조).
  const isAdmin = groups.some((g) => adminGroups.includes(g));
  return (isAdmin ? 'ADMIN' : undefined) as unknown as UserRole;
}

/**
 * Decodes a JWT without verifying the signature.
 *
 * Middleware only checks cookie presence; signature verification is handled
 * server-side by the API. This utility is used client-side / in RSC to read
 * the payload for display and permission gating.
 *
 * @throws {Error} When the token is malformed or the payload cannot be parsed.
 */
export function parseJWT(token: string): AdminSession {
  const parts = token.split('.');

  if (parts.length !== 3) {
    throw new Error('Invalid JWT format: expected 3 dot-separated segments');
  }

  const payloadSegment = parts[1];

  // Base64URL → Base64 → JSON
  const base64 = payloadSegment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');

  let jsonString: string;
  try {
    jsonString = atob(padded);
  } catch {
    throw new Error('Failed to decode JWT payload: invalid base64');
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(jsonString) as Record<string, unknown>;
  } catch {
    throw new Error('Failed to parse JWT payload: invalid JSON');
  }

  return {
    user_id: String(payload['sub'] ?? payload['user_id'] ?? ''),
    email: String(payload['email'] ?? ''),
    display_name: String(payload['display_name'] ?? payload['name'] ?? ''),
    role: resolveRole(payload),
    team_id: (payload['team_id'] as string | null) ?? null,
    department_id: (payload['department_id'] as string | null) ?? null,
    issued_at: String(payload['iat'] ?? payload['issued_at'] ?? ''),
    // exp(표준 NumericDate) 우선, 없으면 expires_at(ISO) — dev-login 토큰은 후자만 갖는다.
    // 폴백이 없으면 dev 토큰의 24h 만료가 어디에서도 읽히지 않아 사실상 무기한이 된다.
    expires_at: String(payload['exp'] ?? payload['expires_at'] ?? ''),
  };
}

/**
 * parseJWT 에 로그인 때 기억해 둔 역할 쿠키(lib/roleCookie.ts)를 더한다.
 *
 * 토큰이 먼저다 — 토큰이 역할을 주면(dev 로그인·내부 토큰·관리자 그룹 구성원) 쿠키는 보지 않으므로
 * 예전과 똑같다(team_id 도 토큰 것). 토큰이 역할을 못 줄 때만 **이 토큰에 묶인** 쿠키를 보고, 토큰에
 * team_id 가 없으면 쿠키의 팀을 쓴다 — IdP 토큰에는 팀이 없어 팀 리더의 예산 화면이 비었다.
 *
 * @throws parseJWT 와 같다 — 토큰을 못 읽으면 던진다(middleware 는 로그인으로 보낸다).
 */
export async function resolveSession(token: string, roleCookie?: string): Promise<AdminSession> {
  const session = parseJWT(token);
  if (session.role) return session;
  const remembered = await readRoleCookie(roleCookie, token);
  if (!remembered) return { ...session, role: undefined as unknown as UserRole };
  return { ...session, role: remembered.role, team_id: session.team_id || remembered.teamId };
}

/**
 * 시계 오차 여유(초). UI 는 admin-api 보다 **먼저** 만료로 판정한다.
 *
 * ⚠️ 방향이 중요하다. admin-api 의 jose `jwt.decode` 는 leeway 를 넘기지 않아 기본값 0 이다
 *    (admin-api/src/app/core/auth.py:63, core/oidc_verifier.py:233) — 즉 exp 시점에 정확히
 *    거절한다. 그러므로 UI 가 exp 보다 **늦게** 만료로 보면(+skew) 그 구간에서 페이지는
 *    열리는데 서버 컴포넌트의 모든 호출이 401 이 되어, 고치려던 바로 그 증상('—' 와
 *    fetchFailed 만 깔린 화면)이 재현된다. 그래서 빼는 것이 맞다.
 *
 *    반대 방향의 대가는 작다: 아직 유효한 토큰을 최대 30초 일찍 로그인으로 보내는 것뿐이고,
 *    실제 서명·만료 판정은 admin-api 가 다시 하므로 여기서의 보수적 판정은 권한 문제가 없다.
 */
const CLOCK_SKEW_SECONDS = 30;

/**
 * 세션이 만료됐는지. **만료를 확신할 수 없으면 false**(=만료 아님) 를 돌려준다.
 *
 * ⚠️ 이 관용이 핵심이다. dev-login 이 발급하는 토큰(`dev.<payload>.sig`)에는 숫자
 *    `exp` 클레임이 아예 없고 ISO 문자열 `expires_at` 만 있어서, parseJWT 를 통과하면
 *    session.expires_at 이 빈 문자열이 된다. 여기서 "값이 없으면 만료"로 판정하면
 *    dev 사용자가 로그인하는 순간 다시 로그인 화면으로 튕겨 무한 리다이렉트가 된다.
 *
 * 배경: 예전엔 만료 검사 자체가 없어서, 만료된 admin_jwt 로도 페이지가 그대로 열리고
 * 서버 컴포넌트의 모든 API 호출만 401 이 됐다 — 화면에는 '—' 와 fetchFailed 만 깔리고
 * 로그인으로 돌아갈 길이 없었다.
 */
export function isSessionExpired(session: AdminSession, nowMs: number = Date.now()): boolean {
  const raw = session.expires_at?.trim();
  if (!raw) return false; // exp 클레임 없음(dev 토큰) → 만료로 취급하지 않는다

  // JWT 표준: NumericDate = epoch **초**. 밀리초로 넣는 발급자도 있어 자릿수로 구분한다.
  let expiresAtMs: number;
  if (/^\d+$/.test(raw)) {
    const n = Number(raw);
    expiresAtMs = n > 1e11 ? n : n * 1000;
  } else {
    expiresAtMs = Date.parse(raw); // ISO 8601 도 받아준다
  }

  if (!Number.isFinite(expiresAtMs)) return false; // 해석 불가 → 판정 보류

  return expiresAtMs - CLOCK_SKEW_SECONDS * 1000 <= nowMs;
}

/**
 * Checks whether `role` is allowed to access the given `pathname`.
 *
 * Uses prefix matching: `/budgets/123` is covered by the `/budgets` entry.
 * Falls back to `false` (deny) for paths not listed in PAGE_PERMISSIONS.
 */
export function checkPagePermission(pathname: string, role: UserRole): boolean {
  // Find the most specific (longest) matching prefix
  const matchingEntry = Object.entries(PAGE_PERMISSIONS)
    .filter(([prefix]) => {
      if (prefix === '/') {
        // Root matches only the exact path '/' to avoid swallowing everything
        return pathname === '/';
      }
      return pathname === prefix || pathname.startsWith(`${prefix}/`);
    })
    .sort(([a], [b]) => b.length - a.length)[0];

  if (!matchingEntry) {
    return false; // No permission entry found — deny by default
  }

  const [, allowedRoles] = matchingEntry;
  return allowedRoles.includes(role);
}
