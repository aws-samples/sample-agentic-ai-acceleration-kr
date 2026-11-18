// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 로그인한 사람의 실효 역할을 admin-api 에 묻는다 — `GET /admin/me`. 서버 전용(Node 런타임, 로그인 콜백).
 *
 * 토큰만으로는 부족하다: admin-api 는 관리자 그룹 외에 이메일과 DB 역할(TEAM_LEADER 등)로도 인가한다.
 *
 * 결과는 세 갈래이고 이 함수는 던지지 않는다.
 *   - ok          — 200 과 알려진 역할.
 *   - denied      — 401/403. admin-api 가 이 토큰·이 사람을 받지 않는다는 **판정**이다.
 *   - unavailable — 판정을 못 받았다(연결 실패·시간 초과·그 밖의 상태·이상한 본문). 이 경로가 없는
 *                   옛 admin-api 는 404 를 준다.
 */

import { UserRole } from '@/types/enums';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

/** 로그인 한 번을 붙잡아 둘 수 있는 상한. */
const TIMEOUT_MS = 5000;

const KNOWN_ROLES: readonly string[] = Object.values(UserRole);

export type IdentityResult =
  | { kind: 'ok'; role: UserRole; email: string; userId: string; teamId: string | null }
  | { kind: 'denied'; status: 401 | 403; code: string }
  | { kind: 'unavailable'; reason: string };

/** admin-api 봉투(`{"error": {"message": ...}}`)의 message, 없으면 FastAPI 원형 `{"detail": "..."}`. */
async function errorCode(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: unknown }; detail?: unknown } | null;
    if (typeof body?.error?.message === 'string') return body.error.message;
    if (typeof body?.detail === 'string') return body.detail;
  } catch {
    // JSON 이 아니다 — 상태코드만으로 판정한다.
  }
  return '';
}

export async function fetchIdentity(token: string): Promise<IdentityResult> {
  let res: Response;
  try {
    res = await fetch(`${ADMIN_API_URL}/admin/me`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    if ((e as { name?: unknown } | null)?.name === 'TimeoutError') {
      return { kind: 'unavailable', reason: `no answer within ${TIMEOUT_MS} ms` };
    }
    return {
      kind: 'unavailable',
      reason: `request failed: ${e instanceof Error ? e.message : String(e)}`,
    };
  }

  const status = res.status;
  if (status === 401 || status === 403) {
    return { kind: 'denied', status, code: await errorCode(res) };
  }
  if (status !== 200) {
    return { kind: 'unavailable', reason: `HTTP ${status}` };
  }

  let body: { role?: unknown; email?: unknown; user_id?: unknown; team_id?: unknown } | null;
  try {
    body = (await res.json()) as typeof body;
  } catch {
    return { kind: 'unavailable', reason: 'HTTP 200 body is not JSON' };
  }

  const role = body?.role;
  if (typeof role !== 'string' || !KNOWN_ROLES.includes(role)) {
    return { kind: 'unavailable', reason: `HTTP 200 with an unknown role (${String(role)})` };
  }
  return {
    kind: 'ok',
    role: role as UserRole,
    email: typeof body?.email === 'string' ? body.email : '',
    userId: typeof body?.user_id === 'string' ? body.user_id : '',
    teamId: typeof body?.team_id === 'string' ? body.team_id : null,
  };
}
