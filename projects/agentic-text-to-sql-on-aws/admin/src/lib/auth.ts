// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * API route 인증·인가 헬퍼.
 *
 * 흐름
 * ----
 * 1. 클라이언트가 `POST /api/auth/login` 으로 받은 Cognito **AccessToken** 을
 *    `Authorization: Bearer <token>` 으로 보낸다.
 * 2. `aws-jwt-verify` 로 서명·발급자·클라이언트·`token_use=access` 를 검증한다.
 *    (검증기는 모듈 레벨 캐시 — JWKS 를 프로세스 수명 동안 재사용한다.)
 * 3. `cognito:groups` 클레임으로 인가한다.
 *    - Manager | Admin 이 아니면 403, 토큰이 없거나 유효하지 않으면 401.
 *    - Admin 전용 경로(`iam/*`)는 Admin 그룹만 허용.
 *
 * 원본 Bearer 토큰은 그대로 보존해 Gateway MCP 호출에 재사용한다(On-Behalf-Of).
 */

import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { requiredEnv } from './env';

/** 관리 콘솔 접근이 허용되는 그룹 (페르소나: Manager / Admin). */
export const MANAGER_GROUPS = ['Manager', 'Admin'] as const;
/** Admin 전용 기능(권한 관리) 그룹. */
export const ADMIN_GROUP = 'Admin';

export interface AdminPrincipal {
  /** Cognito username 클레임 — MCP 도구의 `actor` 인자로 전달(감사 기록). */
  username: string;
  /** Cognito sub (사용자 고유 ID). */
  sub: string;
  /** `cognito:groups` 클레임 (없으면 빈 배열). */
  groups: string[];
  /** 원본 AccessToken — Gateway MCP OBO 호출에 그대로 전달한다. */
  accessToken: string;
  /** Admin 그룹 보유 여부. */
  isAdmin: boolean;
}

/** 인증/인가 실패를 HTTP 상태와 함께 표현하는 오류. */
export class AuthError extends Error {
  readonly status: 401 | 403;

  constructor(status: 401 | 403, message: string) {
    super(message);
    this.name = 'AuthError';
    this.status = status;
  }
}

type Verifier = ReturnType<typeof CognitoJwtVerifier.create>;

let cachedVerifier: Verifier | null = null;

/** AccessToken 검증기 (JWKS 캐시 재사용). */
function getVerifier(): Verifier {
  if (!cachedVerifier) {
    cachedVerifier = CognitoJwtVerifier.create({
      userPoolId: requiredEnv('COGNITO_USER_POOL_ID'),
      tokenUse: 'access',
      clientId: requiredEnv('COGNITO_CLIENT_ID'),
    });
  }
  return cachedVerifier;
}

/** `Authorization: Bearer <token>` 헤더에서 토큰만 추출. */
function extractBearer(request: Request): string {
  const header = request.headers.get('authorization') ?? request.headers.get('Authorization');
  if (!header) throw new AuthError(401, '인증 토큰이 없습니다');
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!match) throw new AuthError(401, 'Authorization 헤더 형식이 올바르지 않습니다');
  return match[1].trim();
}

/** `cognito:groups` 클레임을 문자열 배열로 정규화 (문자열/배열 모두 허용). */
function normalizeGroups(claim: unknown): string[] {
  if (Array.isArray(claim)) return claim.map(String);
  if (typeof claim === 'string') {
    return claim
      .replace(/^\[|\]$/g, '')
      .split(/[,\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);
  }
  return [];
}

/**
 * 토큰을 검증하고 principal 을 만든다. 그룹 검사는 하지 않는다
 * (그룹 요구가 다른 경로를 위해 `requireManager`/`requireAdmin` 로 분리).
 */
export async function authenticate(request: Request): Promise<AdminPrincipal> {
  const accessToken = extractBearer(request);
  let payload: Record<string, unknown>;
  try {
    payload = (await getVerifier().verify(accessToken)) as unknown as Record<string, unknown>;
  } catch (error) {
    throw new AuthError(401, `토큰 검증 실패: ${(error as Error).message}`);
  }
  const groups = normalizeGroups(payload['cognito:groups']);
  return {
    username: String(payload.username ?? payload.sub ?? 'unknown'),
    sub: String(payload.sub ?? ''),
    groups,
    accessToken,
    isAdmin: groups.includes(ADMIN_GROUP),
  };
}

/** Manager 또는 Admin 만 통과 (관리 콘솔 공통 경로). */
export async function requireManager(request: Request): Promise<AdminPrincipal> {
  const principal = await authenticate(request);
  const allowed = principal.groups.some((g) => (MANAGER_GROUPS as readonly string[]).includes(g));
  if (!allowed) {
    throw new AuthError(403, 'Manager 또는 Admin 그룹 권한이 필요합니다');
  }
  return principal;
}

/** Admin 만 통과 (`iam/*` 경로). */
export async function requireAdmin(request: Request): Promise<AdminPrincipal> {
  const principal = await authenticate(request);
  if (!principal.isAdmin) {
    throw new AuthError(403, 'Admin 그룹 권한이 필요합니다');
  }
  return principal;
}
